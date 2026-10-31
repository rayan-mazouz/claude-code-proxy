"""Dashboard team-member authentication and permission boundaries."""

import uuid


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_overview_member_gets_analytics_without_management_access(client, admin_headers):
    password = "member-password-123"
    created = client.post(
        "/api/v1/team/members",
        headers=admin_headers,
        json={
            "username": "viewer",
            "password": password,
            "permissions": ["analytics:read"],
        },
    )
    assert created.status_code == 201, created.text

    headers = _login(client, "viewer", password)
    profile = client.get("/api/v1/auth/me", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["permissions"] == ["analytics:read"]
    assert client.get("/api/v1/lookups/users", headers=headers).status_code == 200
    assert client.get("/api/v1/lookups", headers=headers).status_code == 200
    assert client.get("/api/v1/stats/dashboard", headers=headers).status_code == 200
    assert client.get("/api/v1/accounts", headers=headers).status_code == 403
    assert client.get("/api/v1/users", headers=headers).status_code == 403
    assert client.get("/api/v1/team/members", headers=headers).status_code == 403
    assert client.get(f"/api/v1/requests/{uuid.uuid4()}", headers=headers).status_code == 403


def test_disabling_member_invalidates_existing_session(client, admin_headers):
    password = "member-password-123"
    created = client.post(
        "/api/v1/team/members",
        headers=admin_headers,
        json={
            "username": "operator",
            "password": password,
            "permissions": ["accounts:read", "accounts:write", "analytics:read"],
        },
    )
    member_id = created.json()["member"]["id"]
    headers = _login(client, "operator", password)
    disabled = client.put(
        f"/api/v1/team/members/{member_id}",
        headers=admin_headers,
        json={"active": False},
    )
    assert disabled.status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_legacy_archive_permission_is_returned_under_canonical_name(client, admin_headers):
    created = client.post(
        "/api/v1/team/members",
        headers=admin_headers,
        json={
            "username": "archive-viewer",
            "password": "member-password-123",
            "permissions": ["events:archive:read"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["member"]["permissions"] == ["analytics:events:archive:read"]

    headers = _login(client, "archive-viewer", "member-password-123")
    assert client.get("/api/v1/auth/me", headers=headers).json()["permissions"] == ["analytics:events:archive:read"]
