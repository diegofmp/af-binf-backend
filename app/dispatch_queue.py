"""The HPC dispatch queue: which queued job goes next.

Jobs are dispatched one at a time, for all users (see app.dispatcher). The
order is decided by an *order policy*, chosen with HPC_DISPATCH_ORDER:

  - "fair" (default): round-robin between users, so one user submitting a
    burst of jobs doesn't push everyone else back by that many slots.
  - "fifo": first come, first served.

Both the dispatcher (to pick the next job) and the API (to show a job's queue
position) go through `queue_order`, which applies the configured policy, so
the two can't disagree.

Adding a policy:
  1. Write a function `(QueueSnapshot) -> list[uuid.UUID]` that returns every
     job id in the snapshot exactly once, in dispatch order. It must be pure
     and deterministic (no I/O, same input -> same output), since it runs on
     every claim and every queue-position lookup.
  2. Register it in ORDER_POLICIES and add its name to `hpc_dispatch_order`
     in app.config (a test checks the two match).
  3. If it needs data the snapshot doesn't have yet (e.g. job version, a
     priority), add a field to QueuedJob or QueueSnapshot and load it in
     `_load_snapshot`. Existing policies are unaffected.
"""

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.job import PENDING_DISPATCH_STATUSES, Job, JobStatus


@dataclass(frozen=True)
class QueuedJob:
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class QueueSnapshot:
    """Everything an order policy gets to decide with."""

    # All currently `queued` jobs, in no particular order.
    jobs: list[QueuedJob]
    # For each user with a queued job: when their most recent job was picked
    # up for dispatch (None if never).
    last_dispatch: dict[uuid.UUID, datetime | None]


OrderPolicy = Callable[[QueueSnapshot], list[uuid.UUID]]


def _as_utc(value: datetime) -> datetime:
    # SQLite (tests) hands back naive datetimes; they're UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _oldest_first_key(job: QueuedJob) -> tuple:
    return (_as_utc(job.created_at), str(job.id))


def fifo_order(snapshot: QueueSnapshot) -> list[uuid.UUID]:
    """First come, first served: oldest queued job first, regardless of user."""
    return [job.id for job in sorted(snapshot.jobs, key=_oldest_first_key)]


def fair_order(snapshot: QueueSnapshot) -> list[uuid.UUID]:
    """Round-robin between users.

    Users take turns, starting with the one whose last dispatch is oldest
    (never-dispatched users first, then by their oldest queued job). Each turn
    dispatches that user's oldest queued job and sends them to the back of the
    line, which is exactly what repeatedly picking "the user who waited
    longest" does.
    """
    last_dispatch = snapshot.last_dispatch
    jobs_by_user: dict[uuid.UUID, deque[QueuedJob]] = {}
    for job in sorted(snapshot.jobs, key=_oldest_first_key):
        jobs_by_user.setdefault(job.user_id, deque()).append(job)

    def turn_key(user_id: uuid.UUID) -> tuple:
        last = last_dispatch.get(user_id)
        oldest_job = jobs_by_user[user_id][0]
        return (
            last is not None,
            _as_utc(last) if last is not None else datetime.min.replace(tzinfo=UTC),
            _as_utc(oldest_job.created_at),
            str(user_id),
        )

    turns = deque(sorted(jobs_by_user, key=turn_key))
    order: list[uuid.UUID] = []
    while turns:
        user_id = turns.popleft()
        order.append(jobs_by_user[user_id].popleft().id)
        if jobs_by_user[user_id]:
            turns.append(user_id)
    return order


# name (HPC_DISPATCH_ORDER) -> policy. Keep in sync with
# app.config.Settings.hpc_dispatch_order.
ORDER_POLICIES: dict[str, OrderPolicy] = {
    "fair": fair_order,
    "fifo": fifo_order,
}


async def _load_snapshot(db: AsyncSession) -> QueueSnapshot:
    rows = (
        await db.execute(
            select(Job.id, Job.user_id, Job.created_at).where(Job.status == JobStatus.QUEUED)
        )
    ).all()
    jobs = [QueuedJob(id=r.id, user_id=r.user_id, created_at=r.created_at) for r in rows]
    if not jobs:
        return QueueSnapshot(jobs=[], last_dispatch={})

    user_ids = {job.user_id for job in jobs}
    last_dispatch = dict(
        (
            await db.execute(
                select(Job.user_id, func.max(Job.dispatch_started_at))
                .where(Job.user_id.in_(user_ids))
                .group_by(Job.user_id)
            )
        ).all()
    )
    return QueueSnapshot(jobs=jobs, last_dispatch=last_dispatch)


async def queue_order(db: AsyncSession) -> list[uuid.UUID]:
    """Ids of all `queued` jobs, in the order they'll be dispatched (given no
    new submissions), per the configured HPC_DISPATCH_ORDER policy. The queue
    is capped by HPC_QUEUE_MAX_DEPTH, so loading it whole is cheap.
    """
    snapshot = await _load_snapshot(db)
    if not snapshot.jobs:
        return []
    policy = ORDER_POLICIES[get_settings().hpc_dispatch_order]
    return policy(snapshot)


async def queue_position(db: AsyncSession, job_id: uuid.UUID) -> int | None:
    """1-based position of a queued job (1 = dispatched next), or None if it
    isn't queued. An estimate: with the "fair" policy, a user with no recent
    dispatches who submits later is served before users who were just served,
    so a position can go up as well as down.
    """
    order = await queue_order(db)
    try:
        return order.index(job_id) + 1
    except ValueError:
        return None


async def pending_dispatch_count(db: AsyncSession) -> int:
    """Jobs not yet dispatched (queued + dispatching), across all users."""
    return (
        await db.scalar(
            select(func.count()).select_from(Job).where(Job.status.in_(PENDING_DISPATCH_STATUSES))
        )
    ) or 0
