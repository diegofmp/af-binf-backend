"""The HPC dispatcher: sends queued jobs to the HPC, one at a time.

Run as its own process: `python -m app.dispatcher`.

Every HPC call uses the same SSH service account, so the backend must not
open one connection per user request. POST /jobs only records a `queued` job;
this process drains the queue for all users:

  - one job at a time, over a single reused SSH connection (app.hpc)
  - at least HPC_DISPATCH_MIN_INTERVAL_SECONDS between calls, so the call
    rate to the HPC is fixed no matter how many users submit at once
  - in fair order between users (app.dispatch_queue)
  - if the HPC is unreachable, jobs stay queued and the dispatcher backs off
    (doubling up to HPC_DISPATCH_BACKOFF_MAX_SECONDS) instead of failing them

Only one dispatcher may be active, or the call rate would double. A Postgres
advisory lock enforces that: start a second copy and it waits as a standby,
taking over if the active one dies.
"""

import asyncio
import contextlib
import enum
import signal
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db import engine as app_engine
from app.dispatch_queue import queue_order
from app.hpc import HPCConnection, HPCUnavailableError
from app.logging import configure_logging, get_logger
from app.models.job import Job, JobStatus

logger = get_logger(__name__)

# Arbitrary, fixed: identifies "the dispatcher" lock among any other advisory
# locks on the same database.
_ADVISORY_LOCK_KEY = 0x6166_6469_7370  # "afdisp"

# What a lost or unreachable database raises (asyncpg can surface raw OSErrors
# on connect).
_DB_ERRORS = (SQLAlchemyError, OSError)


class Outcome(enum.Enum):
    IDLE = "idle"  # nothing queued
    SUBMITTED = "submitted"
    FAILED = "failed"
    HPC_UNAVAILABLE = "hpc_unavailable"  # job put back in the queue


async def _claim_next(db) -> Job | None:
    """Move the next job in fair order from `queued` to `dispatching`."""
    for job_id in await queue_order(db):
        job = (
            await db.execute(
                select(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            continue
        job.status = JobStatus.DISPATCHING
        job.dispatch_started_at = datetime.now(UTC)
        await db.commit()
        return job
    await db.rollback()
    return None


async def dispatch_next(session_factory: async_sessionmaker, hpc: HPCConnection) -> Outcome:
    """Dispatch the next queued job, if any, and record the result.

    No DB transaction is open during the SSH call: the claim is committed
    first, so the job shows as `dispatching` while it runs.
    """
    async with session_factory() as db:
        job = await _claim_next(db)
        if job is None:
            return Outcome.IDLE
        log = logger.bind(job_id=str(job.id), user_id=str(job.user_id))
        log.info("dispatcher.dispatch.start")

        try:
            output = await asyncio.to_thread(hpc.dispatch, job)
        except HPCUnavailableError as exc:
            # Nothing was sent: back in the queue, keeping its place. Clearing
            # dispatch_started_at keeps this from counting as the user's turn.
            job.status = JobStatus.QUEUED
            job.dispatch_started_at = None
            await db.commit()
            log.warning("dispatcher.dispatch.hpc_unavailable", error=str(exc))
            return Outcome.HPC_UNAVAILABLE
        except Exception as exc:  # noqa: BLE001 - anything else must fail the job, not strand it
            # Either the script failed (DispatchScriptError) or the connection
            # broke after the command was sent. In the second case we can't
            # tell whether it ran, so failing is safer than sending it twice.
            job.status = JobStatus.FAILED
            job.error_message = str(exc) or type(exc).__name__
            await db.commit()
            log.error("dispatcher.dispatch.failed", error=job.error_message)
            return Outcome.FAILED

        job.status = JobStatus.SUBMITTED
        job.hpc_job_id = output or None
        await db.commit()
        log.info("dispatcher.dispatch.ok")
        return Outcome.SUBMITTED


async def recover_interrupted(session_factory: async_sessionmaker) -> int:
    """Fail jobs left in `dispatching` by a dispatcher that died mid-call.

    Called on becoming the active dispatcher, when no other dispatcher can be
    running. Those jobs' dispatch script may or may not have run, so they're
    failed rather than sent again: a duplicate SLURM job is worse than asking
    the user to resubmit (failed jobs don't count toward their quota).
    """
    async with session_factory() as db:
        result = await db.execute(
            update(Job)
            .where(Job.status == JobStatus.DISPATCHING)
            .values(
                status=JobStatus.FAILED,
                error_message=(
                    "dispatch was interrupted; the job may or may not have reached the "
                    "cluster. Check before resubmitting."
                ),
            )
        )
        await db.commit()
    if result.rowcount:
        logger.warning("dispatcher.recovered_interrupted", count=result.rowcount)
    return result.rowcount or 0


class LeaderLock:
    """Postgres session-level advisory lock held for as long as this process is
    the active dispatcher.

    Uses its own connection from a NullPool engine, so closing it really
    closes it (and frees the lock) rather than returning a lock-holding
    connection to a pool. On non-Postgres databases (SQLite in tests) there is
    no lock and this process is always the leader.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None

    async def ensure(self) -> bool:
        """True if we hold the lock: still, or newly acquired."""
        if self._engine.dialect.name != "postgresql":
            return True
        if self._conn is not None:
            try:
                await self._conn.execute(text("SELECT 1"))
                await self._conn.commit()
                return True
            except _DB_ERRORS as exc:
                logger.error("dispatcher.leader_lock.lost", error=str(exc))
                await self._discard()
        try:
            conn = await self._engine.connect()
        except _DB_ERRORS as exc:
            logger.error("dispatcher.leader_lock.connect_failed", error=str(exc))
            return False
        try:
            acquired = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
            )
            # A session-level lock outlives the transaction; don't sit idle in one.
            await conn.commit()
        except _DB_ERRORS as exc:
            logger.error("dispatcher.leader_lock.acquire_failed", error=str(exc))
            with contextlib.suppress(*_DB_ERRORS):
                await conn.close()
            return False
        if not acquired:
            await conn.close()
            return False
        self._conn = conn
        return True

    async def _discard(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(*_DB_ERRORS):
                await self._conn.close()
            self._conn = None

    async def release(self) -> None:
        if self._conn is not None:
            # If this fails, closing the connection frees the lock anyway.
            with contextlib.suppress(*_DB_ERRORS):
                await self._conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _ADVISORY_LOCK_KEY}
                )
                await self._conn.commit()
            await self._discard()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, waking early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run(
    stop: asyncio.Event,
    session_factory: async_sessionmaker,
    hpc: HPCConnection,
    lock: LeaderLock,
) -> None:
    """Dispatch loop. Returns once `stop` is set (after finishing any dispatch
    in progress).
    """
    is_leader = False
    backoff: float | None = None
    try:
        while not stop.is_set():
            settings = get_settings()

            if not await lock.ensure():
                if is_leader:
                    logger.error("dispatcher.leader.lost")
                    is_leader = False
                    await asyncio.to_thread(hpc.close)
                # Standby: check again every few seconds.
                await _sleep(stop, 5 * settings.hpc_dispatch_poll_interval_seconds)
                continue
            if not is_leader:
                is_leader = True
                logger.info("dispatcher.leader.acquired")
                await recover_interrupted(session_factory)

            try:
                outcome = await dispatch_next(session_factory, hpc)
            except Exception as exc:  # e.g. the database is down
                logger.exception("dispatcher.iteration_failed", error=str(exc))
                await _sleep(stop, settings.hpc_dispatch_poll_interval_seconds)
                continue

            if outcome is Outcome.IDLE:
                await asyncio.to_thread(hpc.close_if_idle, settings.hpc_ssh_idle_close_seconds)
                await _sleep(stop, settings.hpc_dispatch_poll_interval_seconds)
            elif outcome is Outcome.HPC_UNAVAILABLE:
                backoff = (
                    settings.hpc_dispatch_backoff_initial_seconds
                    if backoff is None
                    else min(backoff * 2, settings.hpc_dispatch_backoff_max_seconds)
                )
                logger.warning("dispatcher.backoff", seconds=backoff)
                await _sleep(stop, backoff)
            else:
                backoff = None
                await _sleep(stop, settings.hpc_dispatch_min_interval_seconds)
    finally:
        await asyncio.to_thread(hpc.close)
        await lock.release()
        logger.info("dispatcher.stopped")


async def _main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    settings = get_settings()
    lock_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    logger.info(
        "dispatcher.started",
        mode=settings.hpc_dispatch_mode,
        min_interval_seconds=settings.hpc_dispatch_min_interval_seconds,
    )
    try:
        await run(stop, AsyncSessionLocal, HPCConnection(), LeaderLock(lock_engine))
    finally:
        await lock_engine.dispose()
        await app_engine.dispose()


def main() -> None:
    configure_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
