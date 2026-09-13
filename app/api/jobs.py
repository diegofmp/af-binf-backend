import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.pull_token import get_job_by_pull_api_key
from app.auth.session import get_current_user
from app.config import get_settings
from app.db import get_db
from app.hpc import dispatch_job
from app.logging import get_logger
from app.models.job import Job, JobStatus
from app.models.user import User
from app.schemas.af2 import AF2Input
from app.schemas.af3 import AF3Input
from app.schemas.job import (
    JobCreate,
    JobCreateResponse,
    JobListResponse,
    JobRead,
    JobValidateResponse,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = get_logger(__name__)


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


@router.post("/validate", response_model=JobValidateResponse)
async def validate_job(
    payload: JobCreate,
    current_user: User = Depends(get_current_user),
) -> JobValidateResponse:
    """Validate `input` without creating a job or dispatching anything.

    Lets the frontend show validation errors as their own step, ahead of a
    separate "confirm submission" action that hits POST /jobs. That endpoint
    re-validates from scratch regardless - this is purely a cheaper upfront
    check so a bad payload doesn't have to reach the DB insert + SSH dispatch
    to be caught.
    """
    validated_input = _validated_input(payload)
    return JobValidateResponse(normalized_input=validated_input.model_dump(mode="json"))


@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobCreateResponse:
    """Validate `input`, record the job, and dispatch it to the HPC side over SSH.

    Our part ends at dispatch: make the input JSON pull-able (GET
    /jobs/{id}/input.json) and run the HPC-side script that fetches it,
    parses it, and submits to SLURM - all on that side. Synchronous: the
    request blocks until the dispatch script returns, so a job is either
    `submitted` or `failed` (dispatch failed) by the time this returns; we
    have no visibility into the eventual SLURM outcome here.
    """
    validated_input = _validated_input(payload)

    # af3 inputs don't have a `general` block yet (see app.schemas.af3's TODO),
    # so this is None there for now.
    sample_id = getattr(getattr(validated_input, "general", None), "sample_id", None)

    job = Job(
        user_id=current_user.id,
        version=payload.version,
        name=payload.name,
        sample_id=sample_id,
        status=JobStatus.SUBMITTED,
        input_payload=validated_input.model_dump(mode="json"),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)  # need job.id before dispatch: the HPC side pulls input.json by it

    try:
        dispatch_output = await run_in_threadpool(dispatch_job, job)
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error_message = f"dispatch failed: {exc}"
        await db.commit()
        logger.error("jobs.create.dispatch_failed", job_id=str(job.id), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=job.error_message
        ) from exc

    job.hpc_job_id = dispatch_output or None
    await db.commit()
    await db.refresh(job)

    settings = get_settings()
    pull_url = f"{settings.backend_public_base_url.rstrip('/')}/api/jobs/{job.id}/input.json"

    logger.info("jobs.create.ok", job_id=str(job.id), user_id=str(current_user.id))
    return JobCreateResponse(
        **JobRead.model_validate(job).model_dump(),
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
) -> Job:
    """Get a single job scoped to the current user.

    Returns 404 (not 403) when the job exists but belongs to someone else, so a user can't
    distinguish "not mine" from "doesn't exist" by probing IDs.
    """
    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == current_user.id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job
