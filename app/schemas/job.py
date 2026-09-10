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
    status: JobStatus
    input_payload: dict[str, Any]
    hpc_job_id: str | None
    error_message: str | None
    created_at: datetime
