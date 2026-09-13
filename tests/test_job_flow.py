import uuid

from sqlalchemy import select

from app.models.job import Job, JobStatus, JobVersion
from app.models.user import User

VALID_AF2_INPUT = {
    "general": {"email": "diego@example.com", "ppmsProject": "my_project", "sampleId": "my_sample"},
    "sample": {
        "database": {"database": "full_dbs"},
        "model": {"model": "monomer"},
        "sequence": {"sequence": "MKTAYIAKQRQISFVKSHFSRQLEERLGLI"},
    },
}


async def test_submit_job_success(authed_client, patch_hpc_dispatch):
    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["hpc_job_id"] is not None
    assert body["error_message"] is None

    assert len(patch_hpc_dispatch.calls) == 1
    call = patch_hpc_dispatch.calls[0]
    assert str(call["job_id"]) == body["id"]
    assert call["version"].value == "af2"


async def test_submit_job_records_failure_when_dispatch_fails(authed_client, patch_hpc_dispatch, db_session):
    patch_hpc_dispatch.fail_next = True

    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": VALID_AF2_INPUT,
        },
    )
    assert resp.status_code == 502

    result = await db_session.execute(select(Job).where(Job.name == "test job"))
    job = result.scalar_one()
    assert job.status == JobStatus.FAILED
    assert "simulated dispatch failure" in job.error_message


async def test_validate_job_accepts_valid_input(authed_client, patch_hpc_dispatch, db_session):
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

    # No job created, no dispatch attempted.
    assert len(patch_hpc_dispatch.calls) == 0
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


async def test_user_cannot_access_another_users_job(authed_client):
    other_user_job_id = uuid.uuid4()
    resp = await authed_client.get(f"/jobs/{other_user_job_id}")
    assert resp.status_code == 404


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
