

async def test_me_requires_authentication(client):
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_me_returns_current_user_with_valid_session(authed_client, test_user):
    resp = await authed_client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(test_user.id)
    assert body["email"] == test_user.email


async def test_me_rejects_garbage_session_cookie(client):
    client.cookies.set("af_session", "not-a-real-session-id")
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_logout_clears_session(authed_client):
    resp = await authed_client.get("/auth/me")
    assert resp.status_code == 200

    logout_resp = await authed_client.post("/auth/logout")
    assert logout_resp.status_code == 200

    resp_after = await authed_client.get("/auth/me")
    assert resp_after.status_code == 401


async def test_jobs_route_requires_authentication(client):
    import uuid

    resp = await client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 401


async def test_mutating_request_without_csrf_header_is_rejected(client, test_user):
    import app.auth.session as session_module

    session_id = await session_module.create_session(test_user.id)
    client.cookies.set(session_module.get_settings().session_cookie_name, session_id)

    # Prime the CSRF cookie but deliberately don't send the header.
    await client.get("/auth/me")
    resp = await client.post(
        "/jobs", json={"version": "af2", "name": "x", "input": {}}
    )
    assert resp.status_code == 403
