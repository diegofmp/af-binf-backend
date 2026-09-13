import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.job import JobStatus, JobVersion
from app.schemas.af2 import AF2Input
from app.schemas.af3 import AF3Input


class JobCreate(BaseModel):
    """Request body for POST /jobs.

    `input` is intentionally typed as a plain dict here: the concrete shape
    depends on `version` (af2 -> AF2Input, af3 -> AF3Input). It is validated
    against the matching schema in the submission endpoint via
    `validated_input()`, before any Job row is created. This keeps the
    discriminated validation explicit and gives us full control over the
    error response shape rather than relying on a pydantic discriminated
    union (which would require inlining both schemas under a shared field).

    The frontend sends `input` as nested tabs (a tab's own fields and its
    nested sub-tabs sit as siblings on the same object), e.g. for af2:
    { "general": {...}, "sample": { "database": {...}, "model": {...},
    "sequence": {...} } }. See app/schemas/af2.py and app/schemas/common.py.
    """

    version: JobVersion
    name: str = Field(..., min_length=1, max_length=255)
    input: dict[str, Any]

    def validated_input(self) -> AF2Input | AF3Input:
        if self.version == JobVersion.AF2:
            return AF2Input.model_validate(self.input)
        return AF3Input.model_validate(self.input)


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: JobVersion
    name: str
    sample_id: str | None
    status: JobStatus
    input_payload: dict[str, Any]
    # Stdout of the HPC-side dispatch script, not a SLURM job id - dispatch is
    # synchronous but the eventual `sbatch` submission happens on the HPC side,
    # out of our view. See app.hpc.dispatch_job.
    hpc_job_id: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class JobCreateResponse(JobRead):
    """Response for POST /jobs - adds the URL for pulling `input_payload` back as
    a JSON file from the HPC side (see GET /jobs/{id}/input.json, authenticated
    with the shared HPC_PULL_API_KEY rather than anything returned here).
    """

    input_pull_url: str


class JobValidateResponse(BaseModel):
    """Response for POST /jobs/validate - no job is created, so there's no id/status
    to return. `normalized_input` is the input as `JobCreate.validated_input()`
    produced it (defaults filled in, values coerced), for the frontend to compare
    against what it sent if useful.
    """

    valid: bool = True
    normalized_input: dict[str, Any]


class JobListItem(BaseModel):
    """One row of GET /jobs - just enough for a submissions list, not the full
    (and much heavier) input_payload that JobRead carries.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sample_id: str | None
    status: JobStatus
    created_at: datetime
    updated_at: datetime


class JobListResponse(BaseModel):
    items: list[JobListItem]
    total: int
    page: int
    page_size: int
