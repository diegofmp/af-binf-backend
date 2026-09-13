"""add jobs.sample_id

Denormalized from validated_input.general.sample_id at job creation, so the
submissions list can show it without unpacking input_payload per row. See
app.api.jobs.create_job.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("sample_id", sa.String(length=255), nullable=True))
    op.create_index("ix_jobs_sample_id", "jobs", ["sample_id"], unique=False)
    # Backfill existing af2 rows from input_payload->general->sampleId; af3 rows have
    # no `general` block yet and stay NULL.
    op.execute(
        "UPDATE jobs SET sample_id = input_payload->'general'->>'sampleId' "
        "WHERE input_payload->'general'->>'sampleId' IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_sample_id", table_name="jobs")
    op.drop_column("jobs", "sample_id")
