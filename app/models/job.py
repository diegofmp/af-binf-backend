import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Plain JSON everywhere except Postgres, where it's stored as JSONB (indexable,
# more efficient). Lets tests run against SQLite without a separate schema.
_JSONVariant = JSON().with_variant(JSONB(), "postgresql")

from app.db import Base

if TYPE_CHECKING:
    from app.models.user import User


class JobVersion(str, enum.Enum):
    AF2 = "af2"
    AF3 = "af3"


class JobStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    version: Mapped[JobVersion] = mapped_column(
        Enum(
            JobVersion,
            name="job_version",
            native_enum=False,
            length=8,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Denormalized from validated_input.general.sample_id (see app.api.jobs.create_job),
    # same as `name` above, so the submissions list can show/sort/filter on it without
    # unpacking input_payload per row. Nullable: af3 inputs don't have a `general` block
    # yet (see app.schemas.af3's TODO), so it's None there for now.
    sample_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="job_status",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=JobStatus.SUBMITTED,
        index=True,
    )

    input_payload: Mapped[dict[str, Any]] = mapped_column(_JSONVariant, nullable=False)

    # Stdout of the HPC-side dispatch script (see app.hpc.dispatch_job), not a
    # SLURM job id - we have no synchronous visibility into the eventual
    # `sbatch` submission, which happens entirely on the HPC side. Text, not a
    # short String, since script stdout isn't bounded the way a job id was.
    hpc_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="jobs")
