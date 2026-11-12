"""dispatch queue: queued/dispatching statuses, jobs.dispatch_started_at

POST /jobs now only queues a job; app.dispatcher sends queued jobs to the HPC
one at a time. See app.dispatch_queue.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status IN ('queued', 'dispatching', 'submitted', 'failed')",
    )
    op.add_column(
        "jobs", sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("jobs", "dispatch_started_at")
    # Jobs that never reached the HPC can't be represented in the old schema.
    op.execute(
        "UPDATE jobs SET status = 'failed', "
        "error_message = 'dispatch queue removed by downgrade before this job was dispatched' "
        "WHERE status IN ('queued', 'dispatching')"
    )
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_status", "jobs", "status IN ('submitted', 'failed')")
