import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models.job import Job, JobStatus, JobVersion
from app.models.user import User

VALID_AF2_INPUT = {
    "general": {"email": "diego@example.com", "ppmsProject": "my_project", "sampleId": "my_sample"},
    "sample": {
        "database": {"database": "full_dbs"},
        "model": {"model": "monomer"},
        "sequence": {"sequences": ["MKTAYIAKQRQISFVKSHFSRQLEERLGLI"]},
    },
}


async def test_submit_job_queues_without_dispatching(authed_client, db_session):
    import app.hpc as hpc_module

    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["queue_position"] == 1
    assert body["hpc_job_id"] is None
    assert body["error_message"] is None

    job = await db_session.get(Job, uuid.UUID(body["id"]))
    assert job.status == JobStatus.QUEUED
    assert job.dispatch_started_at is None
    # Nothing in the request path talks to the HPC.
    assert "dispatch_job" not in vars(hpc_module)


async def test_queue_position_spans_users_and_clears_once_dispatched(
    authed_client, db_session
):
    other_user = User(sso_subject="sub-other", email="other@example.com", display_name="Other")
    db_session.add(other_user)
    await db_session.commit()
    # Explicitly earlier: SQLite's CURRENT_TIMESTAMP only has 1s resolution.
    earlier = datetime.now(UTC) - timedelta(minutes=1)
    db_session.add(
        _job_for(other_user.id, status=JobStatus.QUEUED, name="theirs", created_at=earlier)
    )
    await db_session.commit()

    resp = await authed_client.post(
        "/jobs", json={"version": "af2", "name": "mine", "input": VALID_AF2_INPUT}
    )
    job_id = resp.json()["id"]
    assert resp.json()["queue_position"] == 2

    job = await db_session.get(Job, uuid.UUID(job_id))
    job.status = JobStatus.SUBMITTED
    await db_session.commit()

    body = (await authed_client.get(f"/jobs/{job_id}")).json()
    assert body["status"] == "submitted"
    assert body["queue_position"] is None


async def test_submit_rejected_when_queue_full(authed_client, db_session, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "hpc_queue_max_depth", 2)
    other_user = User(sso_subject="sub-other", email="other@example.com", display_name="Other")
    db_session.add(other_user)
    await db_session.commit()
    db_session.add_all(
        [
            _job_for(other_user.id, status=JobStatus.QUEUED),
            _job_for(other_user.id, status=JobStatus.DISPATCHING),
            # Submitted and failed jobs don't take up queue space.
            _job_for(other_user.id, status=JobStatus.SUBMITTED),
            _job_for(other_user.id, status=JobStatus.FAILED),
        ]
    )
    await db_session.commit()

    resp = await authed_client.post(
        "/jobs", json={"version": "af2", "name": "too many", "input": VALID_AF2_INPUT}
    )
    assert resp.status_code == 503
    assert "queue is full" in resp.json()["detail"]
    assert int(resp.headers["Retry-After"]) > 0
    result = await db_session.execute(select(Job).where(Job.name == "too many"))
    assert result.scalar_one_or_none() is None

    monkeypatch.setattr(get_settings(), "hpc_queue_max_depth", 0)
    resp = await authed_client.post(
        "/jobs", json={"version": "af2", "name": "unlimited", "input": VALID_AF2_INPUT}
    )
    assert resp.status_code == 202


async def test_validate_job_accepts_valid_input(authed_client, db_session):
    resp = await authed_client.post(
        "/jobs/validate",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True

    # No job created.
    result = await db_session.execute(select(Job).where(Job.name == "test job"))
    assert result.scalar_one_or_none() is None


async def test_validate_job_rejects_invalid_input(authed_client):
    resp = await authed_client.post(
        "/jobs/validate",
        json={
            "version": "af2",
            "name": "test job",
            "input": {"general": {}},
        },
    )
    assert resp.status_code == 422


async def test_get_job_returns_created_job(authed_client):
    create_resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    job_id = create_resp.json()["id"]

    get_resp = await authed_client.get(f"/jobs/{job_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == job_id


async def test_unknown_job_is_not_found(authed_client):
    resp = await authed_client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_user_cannot_access_another_users_job(authed_client, db_session):
    other_user = User(sso_subject="sub-other", email="other@example.com", display_name="Other")
    db_session.add(other_user)
    await db_session.commit()
    await db_session.refresh(other_user)
    other_job = Job(
        user_id=other_user.id,
        version=JobVersion.AF2,
        name="not mine",
        status=JobStatus.SUBMITTED,
        input_payload=VALID_AF2_INPUT,
    )
    db_session.add(other_job)
    await db_session.commit()
    await db_session.refresh(other_job)

    resp = await authed_client.get(f"/jobs/{other_job.id}")
    # 404, not 403: the same answer as for an unknown id, so ids can't be probed.
    assert resp.status_code == 404
    assert "my_sample" not in resp.text


async def test_input_json_pull_with_valid_api_key(authed_client, client):
    from app.config import get_settings

    create_resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    body = create_resp.json()
    assert body["input_pull_url"].endswith(f"/jobs/{body['id']}/input.json")

    # No session cookie needed - only the shared API key, as a compute node would send.
    api_key = get_settings().hpc_pull_api_key
    pull_resp = await client.get(
        f"/jobs/{body['id']}/input.json",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert pull_resp.status_code == 200
    assert pull_resp.json()["sample"]["model"]["model"] == "monomer"


async def test_input_json_pull_rejects_missing_key(authed_client, client):
    create_resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    job_id = create_resp.json()["id"]

    resp = await client.get(f"/jobs/{job_id}/input.json")
    assert resp.status_code == 401


async def test_list_jobs_returns_only_current_users_jobs_newest_first(authed_client, db_session):
    for name in ("first job", "second job"):
        await authed_client.post(
            "/jobs", json={"version": "af2", "name": name, "input": VALID_AF2_INPUT}
        )

    other_user = User(sso_subject="sub-other", email="other@example.com", display_name="Other")
    db_session.add(other_user)
    await db_session.commit()
    await db_session.refresh(other_user)
    db_session.add(
        Job(
            user_id=other_user.id,
            version=JobVersion.AF2,
            name="not mine",
            status=JobStatus.SUBMITTED,
            input_payload=VALID_AF2_INPUT,
        )
    )
    await db_session.commit()

    resp = await authed_client.get("/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 20
    names_by_id = {item["id"] for item in body["items"]}
    assert len(names_by_id) == 2
    assert set(body["items"][0].keys()) == {"id", "sample_id", "status", "created_at", "updated_at"}
    assert body["items"][0]["sample_id"] == "my_sample"
    # newest first
    assert body["items"][0]["created_at"] >= body["items"][1]["created_at"]


async def test_list_jobs_paginates(authed_client):
    for i in range(3):
        await authed_client.post(
            "/jobs", json={"version": "af2", "name": f"job {i}", "input": VALID_AF2_INPUT}
        )

    resp = await authed_client.get("/jobs", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

    resp = await authed_client.get("/jobs", params={"page": 2, "page_size": 2})
    body = resp.json()
    assert len(body["items"]) == 1


async def test_input_json_pull_rejects_wrong_key(authed_client, client):
    create_resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    job_id = create_resp.json()["id"]

    resp = await client.get(
        f"/jobs/{job_id}/input.json",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401


def _job_for(user_id, *, created_at=None, status=JobStatus.SUBMITTED, name="seeded"):
    job = Job(
        user_id=user_id,
        version=JobVersion.AF2,
        name=name,
        status=status,
        input_payload=VALID_AF2_INPUT,
    )
    if created_at is not None:
        job.created_at = created_at
    return job


async def test_submission_quota_rejects_over_limit(authed_client, db_session):
    from app.config import get_settings

    limit = get_settings().job_quota_max_submissions
    # None of these get dispatched (no dispatcher runs here): queued jobs
    # count toward the quota too.
    for i in range(limit):
        resp = await authed_client.post(
            "/jobs", json={"version": "af2", "name": f"job {i}", "input": VALID_AF2_INPUT}
        )
        assert resp.status_code == 202

    resp = await authed_client.post(
        "/jobs", json={"version": "af2", "name": "one too many", "input": VALID_AF2_INPUT}
    )
    assert resp.status_code == 429
    assert "submission limit reached" in resp.json()["detail"]
    retry_after = int(resp.headers["Retry-After"])
    assert 0 < retry_after <= get_settings().job_quota_window_hours * 3600
    # Rejected before anything was recorded.
    assert await db_session.scalar(select(func.count()).select_from(Job)) == limit


async def test_submission_quota_ignores_failed_old_and_other_users_jobs(
    authed_client, test_user, db_session, monkeypatch
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "job_quota_max_submissions", 2)

    other_user = User(sso_subject="sub-other", email="other@example.com", display_name="Other")
    db_session.add(other_user)
    await db_session.commit()
    long_ago = datetime.now(UTC) - timedelta(hours=get_settings().job_quota_window_hours + 1)
    db_session.add_all(
        [
            _job_for(test_user.id, status=JobStatus.FAILED),
            _job_for(test_user.id, status=JobStatus.FAILED),
            _job_for(test_user.id, created_at=long_ago),
            _job_for(test_user.id, created_at=long_ago),
            _job_for(other_user.id),
            _job_for(other_user.id),
        ]
    )
    await db_session.commit()

    for i in range(2):
        resp = await authed_client.post(
            "/jobs", json={"version": "af2", "name": f"job {i}", "input": VALID_AF2_INPUT}
        )
        assert resp.status_code == 202

    resp = await authed_client.post(
        "/jobs", json={"version": "af2", "name": "job 3", "input": VALID_AF2_INPUT}
    )
    assert resp.status_code == 429


async def test_submission_quota_zero_disables_limit(authed_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "job_quota_max_submissions", 0)
    for i in range(7):
        resp = await authed_client.post(
            "/jobs", json={"version": "af2", "name": f"job {i}", "input": VALID_AF2_INPUT}
        )
        assert resp.status_code == 202


async def test_quota_endpoint_for_new_user(authed_client):
    from app.config import get_settings

    resp = await authed_client.get("/jobs/quota")
    assert resp.status_code == 200
    settings = get_settings()
    assert resp.json() == {
        "limit": settings.job_quota_max_submissions,
        "window_hours": settings.job_quota_window_hours,
        "used": 0,
        "remaining": settings.job_quota_max_submissions,
        "next_slot_at": None,
    }


async def test_quota_endpoint_tracks_usage_and_next_slot(authed_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "job_quota_max_submissions", 2)
    window = timedelta(hours=get_settings().job_quota_window_hours)

    first = await authed_client.post(
        "/jobs", json={"version": "af2", "name": "job 0", "input": VALID_AF2_INPUT}
    )
    first_created = datetime.fromisoformat(first.json()["created_at"])
    if first_created.tzinfo is None:
        first_created = first_created.replace(tzinfo=UTC)

    body = (await authed_client.get("/jobs/quota")).json()
    assert body["used"] == 1
    assert body["remaining"] == 1
    assert datetime.fromisoformat(body["next_slot_at"]) == first_created + window

    await authed_client.post(
        "/jobs", json={"version": "af2", "name": "job 1", "input": VALID_AF2_INPUT}
    )
    body = (await authed_client.get("/jobs/quota")).json()
    assert body["used"] == 2
    assert body["remaining"] == 0

    # The 429's Retry-After points at the same moment as next_slot_at.
    rejected = await authed_client.post(
        "/jobs", json={"version": "af2", "name": "job 2", "input": VALID_AF2_INPUT}
    )
    assert rejected.status_code == 429
    retry_at = datetime.now(UTC) + timedelta(seconds=int(rejected.headers["Retry-After"]))
    assert abs(retry_at - datetime.fromisoformat(body["next_slot_at"])) < timedelta(seconds=5)


async def test_quota_endpoint_when_disabled(authed_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "job_quota_max_submissions", 0)
    await authed_client.post(
        "/jobs", json={"version": "af2", "name": "job", "input": VALID_AF2_INPUT}
    )
    body = (await authed_client.get("/jobs/quota")).json()
    assert body["limit"] is None
    assert body["remaining"] is None
    assert body["next_slot_at"] is None
    assert body["used"] == 1


async def test_quota_endpoint_requires_authentication(client):
    resp = await client.get("/jobs/quota")
    assert resp.status_code == 401
