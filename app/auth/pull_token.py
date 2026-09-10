import secrets
import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.job import Job


async def get_job_by_pull_api_key(
    job_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """Auth dependency for the wget-able job input endpoint.

    Checks a single static, pre-shared API key (`HPC_PULL_API_KEY`) rather than the
    SSO session cookie used everywhere else: the caller is a script on the HPC
    side that may pull a job's input at any point after submission, with no
    interactive login and no per-job handshake to hand it a fresh credential.
    """
    settings = get_settings()
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")

    key = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(key, settings.hpc_pull_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job
