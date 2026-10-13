"""Dashboard authentication hardening: throttling, session revocation, delegation limits, and safe inputs."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import make_url

from app.config import Settings
from app.utils import events, security
from app.utils.postgres import ProxyEventDb
from app.utils.postgres.base import SessionFactory
from app.utils.security.throttle import FailureThrottle, ThrottleRule


def _create_member(client, admin_headers, username: str, permissions: list[str], password: str = "member-password-123") -> str:
    created = client.post(
        "/api/v1/team/members",
        headers=admin_headers,
        json={"username": username, "password": password, "permissions": permissions},
    )
    assert created.status_code == 201, created.text
    return created.json()["member"]["id"]


def _login(client, username: str, password: str):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def _bearer(response) -> dict[str, str]:
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_non_ascii_credentials_are_rejected_not_crashed(client):
    response = _login(client, "admïn", "pässwörd-ünïcode")
    assert response.status_code == 401


def test_repeated_failures_are_throttled_with_retry_after(client, admin_headers):
    _create_member(client, admin_headers, "target", ["analytics:read"])
    for _ in range(10):
        assert _login(client, "target", "wrong-password-000").status_code == 401
    throttled = _login(client, "target", "member-password-123")
    assert throttled.status_code == 429
    assert int(throttled.headers["Retry-After"]) > 0


def test_throttle_window_slides_and_success_clears_the_pair():
    now = [1000.0]
    throttle = FailureThrottle(ThrottleRule(max_failures=2, window_seconds=60), clock=lambda: now[0])
    throttle.record_failure("k")
    throttle.record_failure("k")
    assert throttle.retry_after("k") == 60
    now[0] += 61
    assert throttle.retry_after("k") is None
    throttle.record_failure("k")
    throttle.reset("k")
    assert throttle.retry_after("k") is None


def test_throttle_bounds_tracked_keys():
    throttle = FailureThrottle(ThrottleRule(max_failures=1, window_seconds=60), max_keys=3)
    for index in range(10):
        throttle.record_failure(f"key-{index}")
    assert throttle.retry_after("key-0") is None
    assert throttle.retry_after("key-9") is not None


def test_password_change_revokes_existing_member_sessions(client, admin_headers):
    member_id = _create_member(client, admin_headers, "rotating", ["analytics:read"])
    headers = _bearer(_login(client, "rotating", "member-password-123"))
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    changed = client.put(f"/api/v1/team/members/{member_id}", headers=admin_headers, json={"password": "a-brand-new-password-456"})
    assert changed.status_code == 200, changed.text
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_bearer(_login(client, "rotating", "a-brand-new-password-456"))).status_code == 200


def test_tokens_without_a_session_version_are_rejected(client):
    import jwt

    settings = security.tokens.settings
    now = datetime.now(timezone.utc)
    legacy = jwt.encode(
        {"sub": settings.ADMIN_USERNAME, "role": "admin", "member_id": None, "permissions": ["*"], "iat": now, "exp": now + timedelta(hours=1)},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 401


def test_team_managers_cannot_grant_permissions_they_lack(client, admin_headers):
    _create_member(client, admin_headers, "manager", ["team:members:read", "team:members:write", "analytics:read"])
    manager = _bearer(_login(client, "manager", "member-password-123"))

    escalated = client.post(
        "/api/v1/team/members",
        headers=manager,
        json={"username": "sidekick", "password": "member-password-123", "permissions": ["accounts:write"]},
    )
    assert escalated.status_code == 403

    allowed = client.post(
        "/api/v1/team/members",
        headers=manager,
        json={"username": "viewer2", "password": "member-password-123", "permissions": ["analytics:read"]},
    )
    assert allowed.status_code == 201, allowed.text

    members = client.get("/api/v1/team/members", headers=manager).json()["members"]
    own_id = next(member["id"] for member in members if member["username"] == "manager")
    self_escalation = client.put(f"/api/v1/team/members/{own_id}", headers=manager, json={"permissions": ["accounts:write", "team:members:write"]})
    assert self_escalation.status_code == 403


def test_team_managers_cannot_take_over_more_privileged_members(client, admin_headers):
    _create_member(client, admin_headers, "manager", ["team:members:read", "team:members:write", "team:members:delete"])
    admin_id = _create_member(client, admin_headers, "keyholder", ["api_keys:read", "api_keys:write"])
    manager = _bearer(_login(client, "manager", "member-password-123"))
    assert client.put(f"/api/v1/team/members/{admin_id}", headers=manager, json={"password": "hijacked-password-789"}).status_code == 403
    assert client.delete(f"/api/v1/team/members/{admin_id}", headers=manager).status_code == 403


def test_postgres_url_escapes_credentials():
    settings = Settings(
        POSTGRES_USER="proxy",
        POSTGRES_PASSWORD="p@ss:w/rd%#?",
        POSTGRES_HOST="db.internal",
        POSTGRES_PORT="5432",
        POSTGRES_DB="claude",
        FERNET_KEY="x" * 44,
        ADMIN_PASSWORD="an-admin-password",
        JWT_SECRET="j" * 48,
        DOMAIN="example.com",
    )
    parsed = make_url(settings.get_postgres_uri())
    assert parsed.password == "p@ss:w/rd%#?"
    assert parsed.host == "db.internal"
    assert parsed.database == "claude"


def test_api_responses_carry_security_headers(client):
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_expired_rate_limit_reservations_are_pruned():
    old = datetime.now(timezone.utc) - events.RESERVATION_RETENTION - timedelta(hours=1)
    with SessionFactory() as db:
        for created_at in (old, old, datetime.now(timezone.utc)):
            db.add(ProxyEventDb(id=uuid.uuid4(), request_id=f"reservation_{uuid.uuid4().hex}", event_type="request.reserved", created_at=created_at))
        db.add(ProxyEventDb(id=uuid.uuid4(), request_id="req_keep", event_type="request.received", created_at=old))
        db.commit()
    assert events.prune_rate_limit_reservations(batch_size=1) == 2
    with SessionFactory() as db:
        remaining = sorted(row.event_type for row in db.query(ProxyEventDb).all())
    assert remaining == ["request.received", "request.reserved"]
