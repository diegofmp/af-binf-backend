import uuid

from sqlalchemy import select

from app.models.job import Job, JobStatus


async def test_submit_job_success(authed_client, patch_hpc_submit):
    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": {
                "job_type": "monomer",
                "sequences": [{"id": "A", "sequence": "MKTAYIAKQRQISFVKSHFSRQLEERLGLI"}],
            },
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["hpc_job_id"] is not None
    assert body["error_message"] is None

    assert len(patch_hpc_submit.calls) == 1
    call = patch_hpc_submit.calls[0]
    assert body["id"] in call["remote_workdir"]
    assert "#SBATCH" in call["script_content"]
    assert b"monomer" in call["input_content"]


async def test_submit_job_records_failure_when_sbatch_rejects(authed_client, patch_hpc_submit, db_session):
    patch_hpc_submit.fail_next = True

    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": {
                "job_type": "monomer",
                "sequences": [{"id": "A", "sequence": "MKTAYIAKQRQISFVKSHFSRQLEERLGLI"}],
            },
        },
    )
    assert resp.status_code == 502

    result = await db_session.execute(select(Job).where(Job.name == "test job"))
    job = result.scalar_one()
    assert job.status == JobStatus.FAILED
    assert "simulated sbatch failure" in job.error_message


async def test_get_job_returns_created_job(authed_client):
    create_resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "test job",
            "input": {
                "job_type": "monomer",
                "sequences": [{"id": "A", "sequence": "MKTAYIAKQRQISFVKSHFSRQLEERLGLI"}],
            },
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
