import base64
import hashlib
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oidc import OIDCClient, OIDCError
from app.auth.session import (
    PendingLogin,
    create_session,
    delete_session,
    get_current_user,
    pop_pending_login,
    store_pending_login,
)
from app.config import get_settings
from app.db import get_db
from app.logging import get_logger
from app.models.user import User
from app.schemas.auth import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


@router.get("/login/sso")
async def login(redirect: str | None = Query(default=None)) -> RedirectResponse:
    """Start the OIDC login flow: redirect the browser to the IdP's authorization endpoint.

    Meant to be navigated to directly (top-level browser redirect), not called via fetch/XHR.
    """
    settings = get_settings()
    oidc = OIDCClient()

    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = _generate_pkce_pair()

    await store_pending_login(
        PendingLogin(
            state=state,
            code_verifier=code_verifier,
            post_login_redirect=redirect or settings.oidc_post_login_redirect,
        )
    )

    auth_url = await oidc.authorization_url(state=state, code_challenge=code_challenge)
    return RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """OIDC redirect target: exchange the auth code, create/update the user, and set the session cookie.

    Called by the IdP, not by the frontend directly.
    """
    settings = get_settings()
    pending = await pop_pending_login(state)
    if pending is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid or expired state")

    oidc = OIDCClient()
    try:
        token_response = await oidc.exchange_code(code=code, code_verifier=pending.code_verifier)
        claims = await oidc.validate_id_token(token_response)
    except OIDCError as exc:
        logger.warning("auth.callback.oidc_error", error=str(exc))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SSO login failed") from exc

    sso_subject = claims["sub"]
    email = claims.get("email", "")
    display_name = claims.get("name") or email or sso_subject

    result = await db.execute(select(User).where(User.sso_subject == sso_subject))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(sso_subject=sso_subject, email=email, display_name=display_name)
        db.add(user)
    else:
        user.email = email
        user.display_name = display_name
    await db.commit()
    await db.refresh(user)

    session_id = await create_session(user.id)
    logger.info("auth.callback.session_created", user_id=str(user.id))

    response = RedirectResponse(url=pending.post_login_redirect, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(
    response: Response,
    session_id: str | None = Cookie(default=None, alias=get_settings().session_cookie_name),
) -> dict:
    """Invalidate the current session and clear the session cookie."""
    if session_id:
        await delete_session(session_id)
    response.delete_cookie(key=get_settings().session_cookie_name, path="/")
    return {"detail": "logged out"}


@router.get("/me", response_model=UserRead)
async def me(current_user: User = Depends(get_current_user)) -> User:
    """Return the currently authenticated user; 401 if there's no valid session."""
    return current_user
