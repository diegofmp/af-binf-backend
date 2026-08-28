# af-binf-backend

Self-service AlphaFold (v2/v3) job submission API. Users authenticate via
company SSO (OIDC), submit AF2/AF3 jobs through this API, and the request is
submitted straight to the HPC cluster (SLURM, over SSH) before the response
comes back. Backend only — a separate React frontend consumes this API.

This is deliberately the simplest version that's still useful: no queue, no
background workers, no result/log fetching yet — a job is either `submitted`
or `failed` by the time `POST /jobs` returns. See
[docs/auth.md](docs/auth.md) for the session/CSRF design rationale.

## Stack

FastAPI + Pydantic v2, PostgreSQL (async SQLAlchemy 2.0 + Alembic), Redis
(sessions), Paramiko (SSH to HPC), Authlib (OIDC), structlog.

## Project layout

```
app/
  main.py, config.py, db.py, logging.py
  models/       SQLAlchemy models (User, Job)
  schemas/      Pydantic schemas (AF2Input, AF3Input, job/auth request-response)
  api/          auth.py, jobs.py route handlers
  auth/         oidc.py, session.py (get_current_user), csrf.py
  hpc.py        sbatch script generation + synchronous SSH submit
alembic/        migrations
tests/
docs/auth.md
```

## Local setup

Requires Python 3.11+, Docker, and Docker Compose.

1. Copy the env file and fill in real values later (safe local defaults are
   already set for Docker Compose):

   ```bash
   cp .env.example .env
   ```

2. Bring up the stack (Postgres, Redis, API):

   ```bash
   docker compose up --build
   ```

   The API container runs `alembic upgrade head` automatically on start. The
   API is then available at `http://localhost:8000`, with interactive docs
   at `http://localhost:8000/docs`.

   `POST /jobs` opens a real SSH connection to `HPC_SSH_HOST` and runs
   `sbatch` — point it at a real (or test) SLURM login node before trying it
   against Docker Compose alone.

3. To run without Docker (e.g. for faster local iteration against a
   Dockerized Postgres/Redis only):

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   docker compose up -d postgres redis
   alembic upgrade head
   uvicorn app.main:app --reload
   ```

## Environment variables

See [.env.example](.env.example) for the full list with comments. Key groups:

- **App**: `APP_SECRET_KEY`, `CORS_ALLOWED_ORIGIN` (must be your React app's
  exact origin — CORS credentials mode rejects `*`).
- **Postgres/Redis**: `DATABASE_URL`, `REDIS_URL`.
- **Session/CSRF**: `SESSION_COOKIE_NAME`, `SESSION_TTL_SECONDS`,
  `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`, `CSRF_COOKIE_NAME`,
  `CSRF_HEADER_NAME`.
- **OIDC**: `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
  `OIDC_REDIRECT_URI`, `OIDC_SCOPES`, `OIDC_POST_LOGIN_REDIRECT` — point these
  at your actual SSO provider; nothing about a specific IdP is hardcoded.
- **HPC**: `HPC_SSH_*`, `HPC_REMOTE_WORKDIR`, `HPC_SLURM_PARTITION`/
  `HPC_SLURM_ACCOUNT`.

## Migrations

```bash
alembic upgrade head                       # apply
alembic revision --autogenerate -m "..."   # generate a new one after model changes
alembic downgrade -1                       # roll back one
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use an in-memory SQLite DB (via `aiosqlite`) and a fake Redis (via
`fakeredis`) for sessions. The SSH/`sbatch` call itself is monkeypatched
(see `tests/conftest.py::patch_hpc_submit`) — no external services or real
cluster access required. Coverage includes: the session/`get_current_user`
dependency, AF2/AF3 input validation (accepting valid payloads, rejecting
malformed ones), and the submit request/response flow (success + failure).

## Notes / TODOs

- `app/schemas/af2.py` and `app/schemas/af3.py` are best-effort
  approximations of AlphaFold2/3 input conventions — marked `# TODO: confirm
  against actual pipeline input spec` pending the finalized HPC-side
  pipeline scripts.
- The SLURM `sbatch` script template in `app/hpc.py::build_slurm_script` is a
  placeholder — swap in the real pipeline invocation once available.
- `app/hpc.py` opens a fresh SSH connection per request and blocks the
  request until `sbatch` returns. This is the known limitation to fix next:
  submission should move off the request path onto a queue (Celery) with a
  background worker polling job status, fetching results, and retrying —
  not something a user's browser tab should wait on or need to stay open
  for.
