import json
import math
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.pull_token import get_job_by_pull_api_key
from app.auth.session import get_current_user
from app.config import get_settings
from app.db import get_db
from app.dispatch_queue import pending_dispatch_count, queue_position
from app.logging import get_logger
from app.models.job import Job, JobStatus
from app.models.user import User
from app.schemas.af2 import AF2Input
from app.schemas.af3 import AF3Input
from app.schemas.job import (
    JobCreate,
    JobCreateResponse,
    JobListResponse,
    JobQuotaResponse,
    JobRead,
    JobValidateResponse,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = get_logger(__name__)

# Statuses that count against the per-user quota: everything except `failed`,
# since a failed dispatch never reached the cluster.
_QUOTA_STATUSES = (JobStatus.QUEUED, JobStatus.DISPATCHING, JobStatus.SUBMITTED)

# What 503 "queue full" tells clients to wait before retrying.
_QUEUE_FULL_RETRY_AFTER_SECONDS = 60


def _validated_input(payload: JobCreate) -> AF2Input | AF3Input:
    try:
        return payload.validated_input()
    except ValidationError as exc:
        # Same shape FastAPI itself uses for request-body 422s, so clients
        # that already know how to render that (e.g. per-field messages)
        # handle this identically instead of getting a dumped repr.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_url=False, include_context=False, include_input=False),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid input for {payload.version.value}: {exc}",
        ) from exc


async def _quota_usage(
    db: AsyncSession, user: User, now: datetime
) -> tuple[int, datetime | None]:
    """How many of `user`'s jobs count against the quota right now, and when
    the earliest of them leaves the rolling window (freeing a slot) - None if
    none count. Queued, dispatching and submitted jobs count; failed dispatches
    never reached the cluster.
    """
    window = timedelta(hours=get_settings().job_quota_window_hours)
    used, oldest = (
        await db.execute(
            select(func.count(), func.min(Job.created_at)).where(
                Job.user_id == user.id,
                Job.status.in_(_QUOTA_STATUSES),
                Job.created_at >= now - window,
            )
        )
    ).one()
    if oldest is None:
        return used, None
    if oldest.tzinfo is None:  # SQLite hands back naive datetimes; they're UTC
        oldest = oldest.replace(tzinfo=UTC)
    return used, oldest + window


async def _enforce_submission_quota(db: AsyncSession, user: User) -> None:
    """Reject with 429 if `user` already has the max number of submitted jobs
    in the rolling quota window.

    Locks the user's row first so two concurrent submissions from the same
    user can't both see "4 of 5" and both get through; the lock is held until
    the caller's commit of the new job. SQLite (tests) ignores FOR UPDATE,
    which is fine there.
    """
    settings = get_settings()
    limit = settings.job_quota_max_submissions
    if limit == 0:
        return

    await db.execute(select(User.id).where(User.id == user.id).with_for_update())

    now = datetime.now(UTC)
    used, next_slot_at = await _quota_usage(db, user, now)
    if used < limit:
        return

    retry_after = max(1, math.ceil((next_slot_at - now).total_seconds()))

    logger.info("jobs.create.quota_exceeded", user_id=str(user.id), count=used, limit=limit)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"submission limit reached: at most {limit} jobs per "
            f"{settings.job_quota_window_hours}h"
        ),
        headers={"Retry-After": str(retry_after)},
    )


async def _enforce_queue_depth(db: AsyncSession) -> None:
    """Reject with 503 if too many jobs, across all users, are already waiting
    for dispatch - typically because the HPC has been unreachable for a while.

    A soft cap: not locked, so concurrent submissions can overshoot it by a
    few. That's fine; it only exists to keep the backlog bounded.
    """
    max_depth = get_settings().hpc_queue_max_depth
    if max_depth == 0:
        return
    depth = await pending_dispatch_count(db)
    if depth < max_depth:
        return
    logger.warning("jobs.create.queue_full", depth=depth, max_depth=max_depth)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="the submission queue is full, please try again later",
        headers={"Retry-After": str(_QUEUE_FULL_RETRY_AFTER_SECONDS)},
    )


async def _job_read(db: AsyncSession, job: Job) -> JobRead:
    read = JobRead.model_validate(job)
    if job.status == JobStatus.QUEUED:
        read.queue_position = await queue_position(db, job.id)
    return read


@router.post("/validate", response_model=JobValidateResponse)
async def validate_job(
    payload: JobCreate,
    current_user: User = Depends(get_current_user),
) -> JobValidateResponse:
    """Validate `input` without creating a job or dispatching anything.

    Lets the frontend show validation errors as their own step, ahead of a
    separate "confirm submission" action that hits POST /jobs. That endpoint
    re-validates from scratch regardless - this is purely a cheaper upfront
    check so a bad payload doesn't have to reach the DB insert to be caught.
    """
    validated_input = _validated_input(payload)
    return JobValidateResponse(normalized_input=validated_input.model_dump(mode="json"))


@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    payload: JobCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobCreateResponse:
    """Validate `input` and queue the job for dispatch to the HPC.

    Returns 202 with the job `queued`; nothing is sent to the HPC during the
    request. The dispatcher process (app.dispatcher) later runs the HPC-side
    script that pulls the input JSON (GET /jobs/{id}/input.json) and submits
    to SLURM, moving the job to `dispatching` and then `submitted` or
    `failed`. Poll GET /jobs/{id} to follow it; `queue_position` shows how
    many jobs are ahead while it's queued.
    """
    validated_input = _validated_input(payload)
    await _enforce_submission_quota(db, current_user)
    await _enforce_queue_depth(db)

    # af3 inputs don't have a `general` block yet (see app.schemas.af3's TODO),
    # so this is None there for now.
    sample_id = getattr(getattr(validated_input, "general", None), "sample_id", None)

    job = Job(
        user_id=current_user.id,
        version=payload.version,
        name=payload.name,
        sample_id=sample_id,
        status=JobStatus.QUEUED,
        input_payload=validated_input.model_dump(mode="json"),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    settings = get_settings()
    pull_url = f"{settings.backend_public_base_url.rstrip('/')}/api/jobs/{job.id}/input.json"

    logger.info("jobs.create.queued", job_id=str(job.id), user_id=str(current_user.id))
    return JobCreateResponse(
        **(await _job_read(db, job)).model_dump(),
        input_pull_url=pull_url,
    )


@router.get("", response_model=JobListResponse)
async def list_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    """List the current user's jobs, newest first - backs the "my submissions" view."""
    total = await db.scalar(
        select(func.count()).select_from(Job).where(Job.user_id == current_user.id)
    )
    result = await db.execute(
        select(Job)
        .where(Job.user_id == current_user.id)
        .order_by(Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    jobs = result.scalars().all()
    return JobListResponse(items=jobs, total=total or 0, page=page, page_size=page_size)


@router.get("/quota", response_model=JobQuotaResponse)
async def get_job_quota(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobQuotaResponse:
    """The current user's submission quota, so the frontend can show e.g.
    "3 of 5 used, next free slot at 14:32" before a submit hits the 429.

    Read-only and unlocked, so it's a snapshot: POST /jobs re-checks under a
    lock regardless.
    """
    settings = get_settings()
    limit = settings.job_quota_max_submissions or None
    used, next_slot_at = await _quota_usage(db, current_user, datetime.now(UTC))
    return JobQuotaResponse(
        limit=limit,
        window_hours=settings.job_quota_window_hours,
        used=used,
        remaining=None if limit is None else max(0, limit - used),
        next_slot_at=None if limit is None else next_slot_at,
    )


@router.get("/{job_id}/input.json")
async def download_job_input(job: Job = Depends(get_job_by_pull_api_key)) -> Response:
    """Serve a job's input JSON for `wget`/`curl` from the HPC side, at any point
    after submission.

    Auth is the shared `HPC_PULL_API_KEY`, not the SSO session cookie - a compute
    node can't do an interactive OIDC login, and the pull can happen well after
    the job was created, so there's no fresh per-job credential to hand out here.
    """
    body = json.dumps(job.input_payload, indent=2).encode()
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="input.json"'},
    )


@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobRead:
    """Get a single job scoped to the current user. Poll this after POST /jobs
    to follow the job from `queued` to `submitted` or `failed`.

    Returns 404 (not 403) when the job exists but belongs to someone else, so a user can't
    distinguish "not mine" from "doesn't exist" by probing IDs.
    """
    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == current_user.id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return await _job_read(db, job)
