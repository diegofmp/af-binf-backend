import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest

from app.config import Settings, get_settings
from app.db import AsyncSessionLocal
from app.db import engine as app_engine
from app.dispatch_queue import (
    ORDER_POLICIES,
    QueuedJob,
    QueueSnapshot,
    fair_order,
    fifo_order,
    queue_order,
    queue_position,
)
from app.dispatcher import LeaderLock, Outcome, dispatch_next, recover_interrupted, run
from app.hpc import DispatchScriptError, HPCUnavailableError
from app.models.job import Job, JobStatus, JobVersion
from app.models.user import User

INPUT = {"general": {"sampleId": "s"}}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _user(db, name: str) -> User:
    user = User(sso_subject=f"sub-{name}", email=f"{name}@example.com", display_name=name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _queued(db, user: User, name: str, minutes: int) -> Job:
    job = Job(
        user_id=user.id,
        version=JobVersion.AF2,
        name=name,
        status=JobStatus.QUEUED,
        input_payload=INPUT,
        created_at=T0 + timedelta(minutes=minutes),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def _reload(job: Job) -> Job:
    async with AsyncSessionLocal() as db:
        return await db.get(Job, job.id)


# --- order policies (pure) ---


def _ref(user, minutes):
    return QueuedJob(id=uuid.uuid4(), user_id=user, created_at=T0 + timedelta(minutes=minutes))


def _snapshot(jobs, last_dispatch=None):
    return QueueSnapshot(jobs=jobs, last_dispatch=last_dispatch or {})


def test_fair_order_round_robins_between_users():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    a1, a2, a3 = _ref(a, 0), _ref(a, 1), _ref(a, 2)
    b1, b2 = _ref(b, 3), _ref(b, 4)
    c1 = _ref(c, 5)

    order = fair_order(_snapshot([a3, b2, c1, a1, b1, a2]))

    assert order == [a1.id, b1.id, c1.id, a2.id, b2.id, a3.id]


def test_fair_order_serves_least_recently_dispatched_user_first():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    a1, b1, c1 = _ref(a, 0), _ref(b, 1), _ref(c, 2)

    order = fair_order(
        _snapshot(
            [a1, b1, c1],
            # a was served just now, b an hour ago, c never.
            {a: T0 + timedelta(hours=2), b: T0 + timedelta(hours=1), c: None},
        )
    )

    assert order == [c1.id, b1.id, a1.id]


def test_fifo_order_is_oldest_first_regardless_of_user():
    a, b = uuid.uuid4(), uuid.uuid4()
    a1, a2, b1 = _ref(a, 0), _ref(a, 1), _ref(b, 2)

    # last_dispatch is ignored by fifo.
    order = fifo_order(_snapshot([b1, a2, a1], {a: T0 + timedelta(hours=1), b: None}))

    assert order == [a1.id, a2.id, b1.id]


def test_policy_names_match_settings():
    allowed = set(get_args(Settings.model_fields["hpc_dispatch_order"].annotation))
    assert allowed == set(ORDER_POLICIES)


@pytest.mark.parametrize("name", sorted(ORDER_POLICIES))
def test_every_policy_returns_each_job_once_deterministically(name):
    policy = ORDER_POLICIES[name]
    rng = random.Random(42)
    users = [uuid.uuid4() for _ in range(5)]
    jobs = [_ref(rng.choice(users), rng.randint(0, 30)) for _ in range(40)]
    last_dispatch = {
        u: None if rng.random() < 0.3 else T0 - timedelta(minutes=rng.randint(0, 60)) for u in users
    }

    order = policy(_snapshot(jobs, last_dispatch))

    assert sorted(order, key=str) == sorted((j.id for j in jobs), key=str)
    shuffled = jobs[:]
    rng.shuffle(shuffled)
    assert policy(_snapshot(shuffled, last_dispatch)) == order
    assert policy(_snapshot([])) == []


# --- dispatch_next ---


async def test_dispatch_next_idle_when_nothing_queued(fake_hpc):
    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.IDLE
    assert fake_hpc.calls == []


async def test_dispatch_next_submits_job(db_session, fake_hpc):
    user = await _user(db_session, "a")
    job = await _queued(db_session, user, "j", 0)

    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.SUBMITTED

    job = await _reload(job)
    assert fake_hpc.calls == [job.id]
    assert job.status == JobStatus.SUBMITTED
    assert job.hpc_job_id == "dispatched fake-1"
    assert job.dispatch_started_at is not None
    assert job.error_message is None


async def test_dispatch_next_fails_job_when_script_fails(db_session, fake_hpc):
    user = await _user(db_session, "a")
    job = await _queued(db_session, user, "j", 0)
    fake_hpc.fail_next = DispatchScriptError("dispatch failed (exit 2): bad input")

    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.FAILED

    job = await _reload(job)
    assert job.status == JobStatus.FAILED
    assert job.error_message == "dispatch failed (exit 2): bad input"


async def test_dispatch_next_fails_job_when_outcome_unknown(db_session, fake_hpc):
    # The connection broke after the command was sent: it may have run, so it
    # must not be retried.
    user = await _user(db_session, "a")
    job = await _queued(db_session, user, "j", 0)
    fake_hpc.fail_next = TimeoutError("timed out")

    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.FAILED

    job = await _reload(job)
    assert job.status == JobStatus.FAILED
    assert "timed out" in job.error_message
    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.IDLE


async def test_dispatch_next_requeues_when_hpc_unavailable(db_session, fake_hpc):
    user = await _user(db_session, "a")
    first = await _queued(db_session, user, "first", 0)
    await _queued(db_session, user, "second", 1)
    fake_hpc.fail_next = HPCUnavailableError("connection refused")

    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.HPC_UNAVAILABLE

    first = await _reload(first)
    assert first.status == JobStatus.QUEUED
    assert first.dispatch_started_at is None
    assert first.error_message is None
    # Keeps its place: it's still next.
    async with AsyncSessionLocal() as db:
        assert await queue_position(db, first.id) == 1

    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.SUBMITTED
    assert fake_hpc.calls == [first.id, first.id]


async def test_dispatch_order_is_fair_and_matches_queue_positions(db_session, fake_hpc):
    a = await _user(db_session, "a")
    b = await _user(db_session, "b")
    # a submits a burst, b submits one job afterwards.
    a1 = await _queued(db_session, a, "a1", 0)
    a2 = await _queued(db_session, a, "a2", 1)
    a3 = await _queued(db_session, a, "a3", 2)
    b1 = await _queued(db_session, b, "b1", 3)

    async with AsyncSessionLocal() as db:
        predicted = await queue_order(db)
        assert await queue_position(db, b1.id) == 2

    while await dispatch_next(AsyncSessionLocal, fake_hpc) is not Outcome.IDLE:
        pass

    assert fake_hpc.calls == [a1.id, b1.id, a2.id, a3.id]
    assert fake_hpc.calls == predicted


async def test_dispatch_order_follows_configured_policy(db_session, fake_hpc, monkeypatch):
    monkeypatch.setattr(get_settings(), "hpc_dispatch_order", "fifo")
    a = await _user(db_session, "a")
    b = await _user(db_session, "b")
    a1 = await _queued(db_session, a, "a1", 0)
    a2 = await _queued(db_session, a, "a2", 1)
    b1 = await _queued(db_session, b, "b1", 2)

    async with AsyncSessionLocal() as db:
        assert await queue_position(db, b1.id) == 3

    while await dispatch_next(AsyncSessionLocal, fake_hpc) is not Outcome.IDLE:
        pass

    assert fake_hpc.calls == [a1.id, a2.id, b1.id]


async def test_new_user_goes_ahead_of_user_just_served(db_session, fake_hpc):
    a = await _user(db_session, "a")
    a1 = await _queued(db_session, a, "a1", 0)
    a2 = await _queued(db_session, a, "a2", 1)
    assert await dispatch_next(AsyncSessionLocal, fake_hpc) is Outcome.SUBMITTED

    b = await _user(db_session, "b")
    b1 = await _queued(db_session, b, "b1", 2)

    while await dispatch_next(AsyncSessionLocal, fake_hpc) is not Outcome.IDLE:
        pass

    assert fake_hpc.calls == [a1.id, b1.id, a2.id]


# --- recovery ---


async def test_recover_interrupted_fails_jobs_left_dispatching(db_session):
    user = await _user(db_session, "a")
    stuck = await _queued(db_session, user, "stuck", 0)
    waiting = await _queued(db_session, user, "waiting", 1)
    stuck.status = JobStatus.DISPATCHING
    stuck.dispatch_started_at = T0
    await db_session.commit()

    assert await recover_interrupted(AsyncSessionLocal) == 1

    stuck = await _reload(stuck)
    assert stuck.status == JobStatus.FAILED
    assert "interrupted" in stuck.error_message
    assert (await _reload(waiting)).status == JobStatus.QUEUED


# --- run loop ---


@pytest.fixture
def fast_dispatcher(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hpc_dispatch_min_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "hpc_dispatch_poll_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "hpc_dispatch_backoff_initial_seconds", 0.01)
    monkeypatch.setattr(settings, "hpc_dispatch_backoff_max_seconds", 0.02)


async def _run_until(fake_hpc, condition, timeout=5.0):
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop, AsyncSessionLocal, fake_hpc, LeaderLock(app_engine)))
    try:
        async with asyncio.timeout(timeout):
            while not await condition():
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await task


async def test_run_drains_queue_and_stops(db_session, fake_hpc, fast_dispatcher):
    user = await _user(db_session, "a")
    jobs = [await _queued(db_session, user, f"j{i}", i) for i in range(3)]

    async def all_submitted():
        return all([(await _reload(j)).status == JobStatus.SUBMITTED for j in jobs])

    await _run_until(fake_hpc, all_submitted)

    assert fake_hpc.calls == [j.id for j in jobs]
    assert fake_hpc.closed >= 1  # connection closed on shutdown


async def test_run_recovers_then_retries_after_hpc_unavailable(
    db_session, fake_hpc, fast_dispatcher
):
    user = await _user(db_session, "a")
    stuck = await _queued(db_session, user, "stuck", 0)
    stuck.status = JobStatus.DISPATCHING
    await db_session.commit()
    job = await _queued(db_session, user, "j", 1)
    fake_hpc.fail_next = HPCUnavailableError("down")

    async def submitted():
        return (await _reload(job)).status == JobStatus.SUBMITTED

    await _run_until(fake_hpc, submitted)

    assert fake_hpc.calls == [job.id, job.id]
    assert (await _reload(stuck)).status == JobStatus.FAILED


async def test_run_dispatches_nothing_without_leader_lock(db_session, fake_hpc, fast_dispatcher):
    user = await _user(db_session, "a")
    await _queued(db_session, user, "j", 0)

    class _NotLeader:
        async def ensure(self):
            return False

        async def release(self):
            pass

    stop = asyncio.Event()
    task = asyncio.create_task(run(stop, AsyncSessionLocal, fake_hpc, _NotLeader()))
    await asyncio.sleep(0.1)
    stop.set()
    await task

    assert fake_hpc.calls == []
