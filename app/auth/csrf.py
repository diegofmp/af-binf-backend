import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Auth endpoints that establish or tear down the session cookie itself can't
# carry a CSRF token yet (login/callback are top-level browser navigations
# from the IdP, not fetch() calls the frontend controls).
_EXEMPT_PATHS = {"/auth/login/sso", "/auth/callback"}


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit-cookie CSRF protection.

    On any response, if the client has no CSRF cookie yet, one is issued
    (a random token, readable by JS so it can be echoed back — this cookie is
    NOT the session cookie and carries no auth power on its own). On every
    state-changing request, the value in the `X-CSRF-Token` header must match
    the value in the cookie. Because cross-site requests can't read another
    origin's cookies, an attacker cannot forge a matching header even though
    the cookie itself is sent automatically by the browser.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        cookie_token = request.cookies.get(settings.csrf_cookie_name)

        if request.method not in _SAFE_METHODS and request.url.path not in _EXEMPT_PATHS:
            header_token = request.headers.get(settings.csrf_header_name)
            if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
                return JSONResponse(status_code=403, content={"detail": "CSRF token missing or invalid"})

        response = await call_next(request)

        if not cookie_token:
            new_token = secrets.token_urlsafe(32)
            response.set_cookie(
                key=settings.csrf_cookie_name,
                value=new_token,
                httponly=False,  # must be JS-readable so the frontend can echo it back
                secure=settings.session_cookie_secure,
                samesite=settings.session_cookie_samesite,
                max_age=settings.session_ttl_seconds,
            )

        return response
