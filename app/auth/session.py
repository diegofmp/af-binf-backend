import json
import secrets
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

import redis.asyncio as redis
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.user import User

_OAUTH_STATE_PREFIX = "oauth_state:"
_SESSION_PREFIX = "session:"
_OAUTH_STATE_TTL_SECONDS = 600  # login flow must complete within 10 minutes


@lru_cache
def get_redis() -> redis.Redis:
    settings = get_settings()
    return redis.from_url(settings.redis_url, decode_responses=True)


@dataclass
class PendingLogin:
    state: str
    code_verifier: str
    post_login_redirect: str


async def store_pending_login(pending: PendingLogin) -> None:
    r = get_redis()
    await r.setex(
        f"{_OAUTH_STATE_PREFIX}{pending.state}",
        _OAUTH_STATE_TTL_SECONDS,
        json.dumps(
            {
                "code_verifier": pending.code_verifier,
                "post_login_redirect": pending.post_login_redirect,
            }
        ),
    )


async def pop_pending_login(state: str) -> PendingLogin | None:
    r = get_redis()
    key = f"{_OAUTH_STATE_PREFIX}{state}"
    raw = await r.get(key)
    if raw is None:
        return None
    await r.delete(key)  # single use, prevents state replay
    data = json.loads(raw)
    return PendingLogin(state=state, **data)


@dataclass
class SessionData:
    user_id: str
    created_at: float
    expires_at: float


async def create_session(user_id: uuid.UUID) -> str:
    settings = get_settings()
    session_id = secrets.token_urlsafe(32)
    now = time.time()
    data = SessionData(user_id=str(user_id), created_at=now, expires_at=now + settings.session_ttl_seconds)
    r = get_redis()
    await r.setex(
        f"{_SESSION_PREFIX}{session_id}",
        settings.session_ttl_seconds,
        json.dumps(data.__dict__),
    )
    return session_id


async def get_session(session_id: str) -> SessionData | None:
    r = get_redis()
    raw = await r.get(f"{_SESSION_PREFIX}{session_id}")
    if raw is None:
        return None
    return SessionData(**json.loads(raw))


async def refresh_session(session_id: str) -> None:
    """Slide the session TTL forward on activity."""
    settings = get_settings()
    r = get_redis()
    await r.expire(f"{_SESSION_PREFIX}{session_id}", settings.session_ttl_seconds)


async def delete_session(session_id: str) -> None:
    r = get_redis()
    await r.delete(f"{_SESSION_PREFIX}{session_id}")


_session_cookie_alias = get_settings().session_cookie_name


async def get_current_user(
    session_id: str | None = Cookie(default=None, alias=_session_cookie_alias),
    db: AsyncSession = Depends(get_db),
) -> User:
    if session_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired or invalid")

    result = await db.execute(select(User).where(User.id == uuid.UUID(session.user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")

    await refresh_session(session_id)
    return user
