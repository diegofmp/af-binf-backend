# af-binf-backend

Self-service AlphaFold (v2/v3) job submission API. Users authenticate via
company SSO (OIDC), submit AF2/AF3 jobs through this API, and a dispatcher
process hands them to the HPC cluster (SLURM, over SSH). Backend only — a
separate React frontend consumes this API.

`POST /jobs` returns `202` with the job `queued`. The dispatcher then moves
it to `dispatching` and on to `submitted` or `failed`; the frontend polls
`GET /jobs/{id}` (which includes `queue_position` while queued). No
result/log fetching yet. See [docs/auth.md](docs/auth.md) for the
session/CSRF design rationale.

### Why a dispatch queue

Every HPC call logs in as the same SSH service account, so the cluster sees
all users as one client. Instead of each request opening its own SSH
connection, a single dispatcher (`python -m app.dispatcher`) drains the queue
for everyone:

- **One call at a time, at a fixed pace**: at least
  `HPC_DISPATCH_MIN_INTERVAL_SECONDS` between calls, over one reused SSH
  connection. More concurrent users means a longer queue, not more load on
  the HPC.
- **Fair between users**: by default the next job comes from the user who
  has waited longest since their last dispatch (round-robin), so one user's
  burst doesn't hold everyone else back. The ordering is a pluggable policy
  (`HPC_DISPATCH_ORDER`: `fair` or `fifo`); see `app/dispatch_queue.py` for
  how to add one.
- **Exactly one active dispatcher**: a Postgres advisory lock; extra copies
  wait as standbys.
- **HPC down**: jobs stay `queued` and the dispatcher backs off
  (`HPC_DISPATCH_BACKOFF_*`) instead of failing them. `HPC_QUEUE_MAX_DEPTH`
  bounds the backlog (`POST /jobs` returns 503 past it).
- **No double submissions**: a job is only retried if the SSH connection
  couldn't be opened (nothing was sent). If the connection breaks after the
  command was sent, or the dispatcher dies mid-call, the job is marked
  `failed` rather than risk a duplicate SLURM job.

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
  auth/         oidc.py, session.py (get_current_user), csrf.py,
                pull_token.py (HPC-side auth for GET /jobs/{id}/input.json)
  hpc.py        SSH dispatch only - runs the HPC-side script as
                `<script> <job_id>`; that script pulls the input JSON itself
                and owns sbatch generation/submission (see Notes/TODOs below)
  dispatch_queue.py  fair ordering of queued jobs (shared by API + dispatcher)
  dispatcher.py      the dispatcher process: `python -m app.dispatcher`
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

   `APP_SECRET_KEY`, `DATABASE_URL`, `OIDC_CLIENT_SECRET`, and
   `HPC_PULL_API_KEY` have no built-in defaults - the app fails fast at
   startup if any of them is unset, rather than silently running with an
   insecure placeholder. `.env.example` already fills in working local
   values for all four; just make sure your `.env` (or whatever injects env
   vars in a given environment) actually sets them.

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
- **HPC**: `HPC_SSH_*` (connection), `HPC_DISPATCH_SCRIPT_AF2`/
  `HPC_DISPATCH_SCRIPT_AF3` (script path run over SSH as `<script> <job_id>`),
  `HPC_PULL_API_KEY`/`BACKEND_PUBLIC_BASE_URL` (lets that script pull the
  job's input JSON back from us). For local testing without cluster access,
  `HPC_DISPATCH_MODE=fake` skips SSH and reports success after
  `HPC_FAKE_DISPATCH_DELAY_SECONDS` (refused when `APP_ENV=production`).
- **Dispatch queue**: `HPC_DISPATCH_MIN_INTERVAL_SECONDS` (default 10 →
  at most 360 dispatches/hour), `HPC_DISPATCH_ORDER` (`fair`/`fifo`),
  `HPC_DISPATCH_POLL_INTERVAL_SECONDS`,
  `HPC_DISPATCH_BACKOFF_INITIAL_SECONDS`/`_MAX_SECONDS`,
  `HPC_SSH_IDLE_CLOSE_SECONDS`, `HPC_QUEUE_MAX_DEPTH` (default 200, `0`
  disables).
- **Quota**: `JOB_QUOTA_MAX_SUBMISSIONS` (default 5, `0` disables) per user
  per rolling `JOB_QUOTA_WINDOW_HOURS` (default 24). Queued jobs count;
  failed dispatches don't. Over the limit, `POST /jobs` returns 429 with a
  `Retry-After` header (seconds). `GET /jobs/quota` reports the current
  user's usage for the frontend.

## Build & deploy

`.github/workflows/build-and-push.yml` builds the image from the `Dockerfile`
and pushes it to Harbor (`harbor.prod.binf1.boku.ac.at/alphafold/af-binf-backend`)
on every push to `main` and on `v*` tags, tagged with both the commit SHA and
`latest`. That's the only artifact CI produces - Postgres and Redis are not
part of the image; they're external dependencies the target environment must
already provide, wired up purely through `DATABASE_URL`/`REDIS_URL`.

For a Kubernetes deployment, the backend expects:

- An existing Postgres reachable via `DATABASE_URL` (this repo doesn't run or
  manage Postgres).
- A Redis instance for session storage, reachable via `REDIS_URL` - a small,
  single-replica deployment is enough (session data only, no durability
  requirement).
- All other settings from `.env.example` provided as a ConfigMap (plain
  values) and Secret (`APP_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`,
  `OIDC_CLIENT_SECRET`, `HPC_PULL_API_KEY`, `HPC_SSH_PASSWORD`) mounted as
  env vars - `app/config.py` reads them the same way regardless of source.
- The HPC SSH private key (`HPC_SSH_PRIVATE_KEY_PATH`), if used instead of a
  password, mounted from a Secret as a file, not passed as an env var value.

k8s manifests for this aren't in the repo yet.

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
`fakeredis`) for sessions. The dispatcher tests use a fake HPC connection
(`tests/conftest.py::FakeHPC`) — no external services or real cluster access
required. Coverage includes: the session/`get_current_user` dependency,
AF2/AF3 input validation (accepting valid payloads, rejecting malformed
ones), the submit/queue flow, fair dispatch ordering, and the dispatcher's
success/failure/HPC-down/crash-recovery paths. The Postgres advisory lock
isn't exercised under SQLite.

## Notes / TODOs

- `app/schemas/af2.py` and `app/schemas/af3.py` are best-effort
  approximations of AlphaFold2/3 input conventions — marked `# TODO: confirm
  against actual pipeline input spec` pending the finalized HPC-side
  pipeline scripts.
- Our responsibility ends at dispatch: `app/hpc.py::HPCConnection.dispatch`
  runs the HPC-side script (`HPC_DISPATCH_SCRIPT_AF2`/`_AF3`) over SSH with
  the job id as its only argument. That script pulls the input JSON itself
  (GET `/api/jobs/{id}/input.json`), parses it, and builds/submits the
  `sbatch` job — none of that lives in this repo.
- Deployments need the dispatcher running alongside the API (same image,
  command `python -m app.dispatcher`). Without it, jobs stay `queued`
  forever. k8s: a Deployment with `replicas: 1` (a second replica is only a
  standby) and `terminationGracePeriodSeconds` ≥ 90.
