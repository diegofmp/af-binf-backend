import itertools
import os
import tempfile
import uuid

# Must run before any `app.*` module is imported: several modules read
# settings at import time (e.g. app.db creates its engine eagerly), so env
# vars have to be in place first.
_tmp_root = tempfile.mkdtemp(prefix="af-binf-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp_root}/test.db"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["APP_SECRET_KEY"] = "test-secret"

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.api.jobs as jobs_module
import app.auth.session as session_module
from app.db import Base
from app.db import engine as app_engine
from app.models.user import User


@pytest_asyncio.fixture(autouse=True)
async def _fresh_schema():
    async with app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def fake_redis():
    import fakeredis.aioredis

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return redis_client


@pytest_asyncio.fixture(autouse=True)
async def _patch_redis(fake_redis, monkeypatch):
    monkeypatch.setattr(session_module, "get_redis", lambda: fake_redis)
    yield


class _FakeHPCDispatch:
    """Stub the SSH dispatch call so tests never touch a real cluster.

    Records each call so tests can assert on what would have been dispatched,
    and returns fake dispatch-script stdout. Set `.fail_next = True` to
    simulate the dispatch script failing on the next call.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_next = False
        self._counter = itertools.count(1)

    def __call__(self, job):
        self.calls.append({"job_id": job.id, "version": job.version})
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated dispatch failure")
        return f"dispatched fake-{next(self._counter)}"


@pytest.fixture(autouse=True)
def patch_hpc_dispatch(monkeypatch):
    fake_dispatch = _FakeHPCDispatch()
    monkeypatch.setattr(jobs_module, "dispatch_job", fake_dispatch)
    return fake_dispatch


@pytest_asyncio.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_session():
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def test_user(db_session) -> User:
    user = User(
        sso_subject=f"sub-{uuid.uuid4()}",
        email="researcher@example.com",
        display_name="Test Researcher",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def authed_client(client, test_user):
    """An AsyncClient with a real session cookie for `test_user`, plus a
    primed CSRF cookie so tests can send the matching `X-CSRF-Token` header
    on POST/PATCH/DELETE (double-submit pattern, see app/auth/csrf.py)."""
    session_id = await session_module.create_session(test_user.id)
    settings = session_module.get_settings()
    client.cookies.set(settings.session_cookie_name, session_id)

    # Any response issues a CSRF cookie if one isn't set yet.
    await client.get("/auth/me")
    csrf_token = client.cookies.get(settings.csrf_cookie_name)
    client.headers[settings.csrf_header_name] = csrf_token

    yield client
