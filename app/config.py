from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    app_secret_key: str = "insecure-dev-key-change-me"
    log_level: str = "INFO"

    cors_allowed_origin: str = "http://localhost:5173"

    database_url: str = "postgresql+asyncpg://af_binf:af_binf@localhost:5432/af_binf"

    redis_url: str = "redis://localhost:6379/0"

    session_cookie_name: str = "af_session"
    session_ttl_seconds: int = 86400
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    csrf_cookie_name: str = "af_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    oidc_issuer: str = "https://sso.example.com/realms/example"
    oidc_client_id: str = "af-binf-backend"
    oidc_client_secret: str = "change-me"
    oidc_redirect_uri: str = "http://localhost:8000/auth/callback"
    oidc_scopes: str = "openid profile email"
    oidc_post_login_redirect: str = "http://localhost:5173/"

    hpc_ssh_host: str = "hpc-login.example.internal"
    hpc_ssh_port: int = 22
    hpc_ssh_username: str = "svc-af-binf"
    hpc_ssh_private_key_path: str | None = None
    hpc_ssh_password: str | None = None
    hpc_remote_workdir: str = "/scratch/af-binf-jobs"
    hpc_slurm_partition: str = "gpu"
    hpc_slurm_account: str | None = None

    @property
    def oidc_scopes_list(self) -> list[str]:
        return self.oidc_scopes.split()


@lru_cache
def get_settings() -> Settings:
    return Settings()
