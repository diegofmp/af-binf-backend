from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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

    # "fake" skips SSH entirely: dispatch just sleeps for
    # `hpc_fake_dispatch_delay_seconds` and reports success, so the submit flow
    # can be exercised locally without reaching the cluster. Jobs end up
    # `submitted` (and count toward the quota) but never run. Refused when
    # APP_ENV=production.
    hpc_dispatch_mode: Literal["ssh", "fake"] = "ssh"
    hpc_fake_dispatch_delay_seconds: float = Field(default=3.0, ge=0)

    # Dispatch queue (see app.dispatcher). POST /jobs only queues a job; a single
    # dispatcher process sends queued jobs to the HPC one at a time, for all
    # users, so the call rate to the shared SSH account doesn't depend on how
    # many users submit at once.
    # Minimum gap between two dispatch calls to the HPC. This is the setting to
    # agree on with the HPC admins: it caps dispatches at 3600/N per hour.
    hpc_dispatch_min_interval_seconds: float = Field(default=10.0, ge=0)
    # Which queued job goes next (app.dispatch_queue.ORDER_POLICIES): "fair" is
    # round-robin between users, "fifo" is first come, first served. Read by
    # both the dispatcher and the API (queue positions); restart both after
    # changing it.
    hpc_dispatch_order: Literal["fair", "fifo"] = "fair"
    # How often the dispatcher checks for new queued jobs when the queue is empty.
    hpc_dispatch_poll_interval_seconds: float = Field(default=2.0, gt=0)
    # While the HPC is unreachable, jobs stay queued and the dispatcher waits
    # before trying again: starting at `initial`, doubling up to `max`.
    hpc_dispatch_backoff_initial_seconds: float = Field(default=15.0, gt=0)
    hpc_dispatch_backoff_max_seconds: float = Field(default=300.0, gt=0)
    # Close the reused SSH connection after this long with no dispatch.
    hpc_ssh_idle_close_seconds: float = Field(default=300.0, gt=0)
    # Max jobs waiting to be dispatched (queued + dispatching), across all
    # users. Beyond that, POST /jobs returns 503 so the backlog can't grow
    # without bound while the HPC is down. 0 disables the cap.
    hpc_queue_max_depth: int = Field(default=200, ge=0)

    # Per-user submission quota: at most `job_quota_max_submissions` jobs
    # (queued, dispatching or submitted) in any rolling `job_quota_window_hours`
    # window. Failed dispatches don't count. 0 disables the limit.
    job_quota_max_submissions: int = Field(default=5, ge=0)
    job_quota_window_hours: int = Field(default=24, ge=1)

    @model_validator(mode="after")
    def _no_fake_dispatch_in_production(self) -> "Settings":
        if self.hpc_dispatch_mode == "fake" and self.app_env == "production":
            raise ValueError("HPC_DISPATCH_MODE=fake is not allowed with APP_ENV=production")
        return self

    @property
    def oidc_scopes_list(self) -> list[str]:
        return self.oidc_scopes.split()


@lru_cache
def get_settings() -> Settings:
    return Settings()
