from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.jobs import router as jobs_router
from app.auth.csrf import CSRFMiddleware
from app.config import get_settings
from app.logging import configure_logging

settings = get_settings()
configure_logging()

app = FastAPI(
    title="AF Binf Backend",
    description="Self-service AlphaFold (v2/v3) job submission API",
    version="0.1.0",
)

# allow_credentials=True requires an explicit origin (never "*") so the
# browser will actually send/accept the httponly session cookie cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.cors_allowed_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", settings.csrf_header_name],
)

app.add_middleware(CSRFMiddleware)

app.include_router(auth_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok"}
