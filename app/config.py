from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    # No default: a missing value should fail startup loudly rather than sign
    # sessions/CSRF tokens with a well-known key.
    app_secret_key: str
    log_level: str = "INFO"

    cors_allowed_origin: str = "http://localhost:5173"

    # No default: a missing value should fail startup rather than silently
    # point at the local dev database.
    database_url: str

    redis_url: str = "redis://localhost:6379/0"

    session_cookie_name: str = "af_session"
    session_ttl_seconds: int = 86400
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    csrf_cookie_name: str = "af_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    oidc_issuer: str = "https://sso.example.com/realms/example"
    oidc_client_id: str = "af-binf-backend"
    # No default: a missing value should fail startup rather than run with a
    # publicly-known placeholder secret.
    oidc_client_secret: str
    oidc_redirect_uri: str = "http://localhost:8000/api/auth/callback"
    oidc_scopes: str = "openid profile email"
    oidc_post_login_redirect: str = "http://localhost:5173/"

    # Base URL the HPC side can reach to pull a job's input JSON over HTTP
    # (see app.auth.pull_token) - not necessarily the same as any browser-facing URL.
    backend_public_base_url: str = "http://localhost:8000"
    # Static, pre-shared credential the HPC side sends as `Authorization: Bearer
    # <key>` to GET /jobs/{id}/input.json. One key for every job, configured out
    # of band on both ends - there's no per-job handshake to deliver a token
    # through, since the pull can happen anytime after submission.
    # No default: a missing value should fail startup rather than run with a
    # publicly-known placeholder key.
    hpc_pull_api_key: str

    hpc_ssh_host: str = "hpc-login.example.internal"
    hpc_ssh_port: int = 22
    hpc_ssh_username: str = "svc-af-binf"
    hpc_ssh_private_key_path: str | None = None
    hpc_ssh_password: str | None = None

    # Absolute path, on the HPC side, of the script we invoke over SSH as
    # `<script> <job_id>` to dispatch a job. One per pipeline version, since
    # af2/af3 need different HPC-side entrypoints. Everything past that point
    # (pulling the input JSON, parsing it, building/submitting the sbatch
    # script) is the script's responsibility, not ours.
    hpc_dispatch_script_af2: str = "/opt/af-binf/dispatch_af2.sh"
    hpc_dispatch_script_af3: str = "/opt/af-binf/dispatch_af3.sh"

    @property
    def oidc_scopes_list(self) -> list[str]:
        return self.oidc_scopes.split()


@lru_cache
def get_settings() -> Settings:
    return Settings()
