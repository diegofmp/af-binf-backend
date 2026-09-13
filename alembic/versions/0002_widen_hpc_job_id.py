"""widen jobs.hpc_job_id to text

`hpc_job_id` now holds the HPC-side dispatch script's stdout rather than a
short SLURM job id (see app.hpc.dispatch_job), so it's no longer bounded the
way a job id was.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("jobs", "hpc_job_id", type_=sa.Text(), existing_type=sa.String(length=64))


def downgrade() -> None:
    op.alter_column("jobs", "hpc_job_id", type_=sa.String(length=64), existing_type=sa.Text())
