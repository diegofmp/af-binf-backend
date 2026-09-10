import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import get_current_user
from app.db import get_db
from app.hpc import build_slurm_script, remote_workdir_for, submit_slurm_job
from app.logging import get_logger
from app.models.job import Job, JobStatus
from app.models.user import User
from app.schemas.job import JobCreate, JobRead

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = get_logger(__name__)


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """Validate `input`, build the AF2/AF3 input JSON + sbatch script, and submit to SLURM over SSH.

    Synchronous: the request blocks until `sbatch` accepts (or rejects) the job. There's no queue
    yet - a job is either `submitted` or `failed` by the time this returns.
    """
    try:
        validated_input = payload.validated_input()
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

    job = Job(
        user_id=current_user.id,
        version=payload.version,
        name=payload.name,
        status=JobStatus.SUBMITTED,
        input_payload=validated_input.model_dump(mode="json"),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)  # need job.id for the remote workdir path below

    remote_workdir = remote_workdir_for(job.id)
    input_bytes = json.dumps(job.input_payload, indent=2).encode()
    script_content = build_slurm_script(job, remote_workdir=remote_workdir)

    try:
        hpc_job_id = await run_in_threadpool(
            submit_slurm_job,
            remote_workdir=remote_workdir,
            job_name=f"af-{job.id}",
            script_content=script_content,
            input_content=input_bytes,
        )
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error_message = f"submission failed: {exc}"
        await db.commit()
        logger.error("jobs.create.submit_failed", job_id=str(job.id), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=job.error_message
        ) from exc

    job.hpc_job_id = hpc_job_id
    await db.commit()
    await db.refresh(job)

    logger.info("jobs.create.ok", job_id=str(job.id), user_id=str(current_user.id), hpc_job_id=hpc_job_id)
    return job


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
