# Path: tests/conftest.py
# Description: Pytest fixtures -- an ephemeral Postgres (testcontainers) and strong throwaway secrets configured before
#              the app is imported, a fresh schema per test, a TestClient, admin auth, and account/user seeding.

import atexit
import os
import secrets as _secrets

from cryptography.fernet import Fernet
from testcontainers.postgres import PostgresContainer

TEST_ADMIN_PASSWORD = "test-admin-password-123"

# Start an ephemeral Postgres and point the app at it BEFORE importing the app, alongside strong throwaway secrets so
# the config guard is satisfied. This keeps the suite hermetic -- no operator infrastructure or .env is required.
_POSTGRES = PostgresContainer("postgres:16-alpine")
_POSTGRES.start()
atexit.register(_POSTGRES.stop)

os.environ.update(
    {
        "POSTGRES_HOST": _POSTGRES.get_container_host_ip(),
        "POSTGRES_PORT": str(_POSTGRES.get_exposed_port(5432)),
        "POSTGRES_USER": _POSTGRES.username,
        "POSTGRES_PASSWORD": _POSTGRES.password,
        "POSTGRES_DB": _POSTGRES.dbname,
        "FERNET_KEY": Fernet.generate_key().decode(),
        "JWT_SECRET": _secrets.token_urlsafe(48),
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": TEST_ADMIN_PASSWORD,
        "DOMAIN": "localhost",
        "EGRESS_TARGETS_JSON": "",
        "POOL_WAIT_TIMEOUT_SECONDS": "0",
        # Unit tests must never connect to the operator's live MinIO archive.
        "ARCHIVE_ENABLED": "false",
        # Exercise warm-up behavior explicitly; production defaults it off to
        # avoid synthetic requests consuming provider quota during incidents.
        "WARMUP_ENABLED": "true",
        "FALLBACK_GENERATION_CANARY_ENABLED": "false",
    }
)

import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Optional  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.main import app  # noqa: E402
from app.utils import crypto, security  # noqa: E402
from app.utils.models.api import AccountStatus  # noqa: E402
from app.utils.postgres import AccountDb, DatabaseBase  # noqa: E402
from app.utils.postgres.base import SessionFactory, engine  # noqa: E402


@pytest.fixture(autouse=True)
def reset_db():
    """Recreate the schema from the ORM models before each test (the native enum is dropped explicitly)."""
    DatabaseBase.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TYPE IF EXISTS account_status"))
        conn.execute(text("DROP TYPE IF EXISTS provider_health"))
    DatabaseBase.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_password():
    return TEST_ADMIN_PASSWORD


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {security.issue_admin_token()}"}


@pytest.fixture
def seed_account():
    def _make(
        label: str = "acct",
        *,
        status: AccountStatus = AccountStatus.ACTIVE,
        session_used_pct: float = 0.0,
        weekly_used_pct: float = 0.0,
        five_hour_rotation_threshold: Optional[float] = None,
        weekly_rotation_threshold: Optional[float] = None,
        rotation_threshold: Optional[float] = None,
        priority: Optional[int] = None,
        expires_in_hours: int = 1,
    ) -> uuid.UUID:
        now = datetime.now(timezone.utc)
        account = AccountDb(
            id=uuid.uuid4(),
            label=label,
            access_token_enc=crypto.encrypt("upstream-access-token"),
            refresh_token_enc=crypto.encrypt("upstream-refresh-token"),
            expires_at=now + timedelta(hours=expires_in_hours),
            status=status,
            session_used_pct=session_used_pct,
            weekly_used_pct=weekly_used_pct,
            priority=priority or 1,
            created_at=now,
            updated_at=now,
        )
        if rotation_threshold is not None:
            account.rotation_threshold = rotation_threshold
            account.five_hour_rotation_threshold = rotation_threshold
            account.weekly_rotation_threshold = rotation_threshold
        if five_hour_rotation_threshold is not None:
            account.five_hour_rotation_threshold = five_hour_rotation_threshold
        if weekly_rotation_threshold is not None:
            account.weekly_rotation_threshold = weekly_rotation_threshold
        with SessionFactory() as db:
            if priority is None:
                account.priority = db.query(AccountDb).count() + 1
            db.add(account)
            db.commit()
            return account.id

    return _make


@pytest.fixture
def make_user(client, admin_headers):
    def _make(name: str = "user", *, fallback_enabled: bool = False, spare_capacity_only: bool = False) -> str:
        created = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"name": name, "fallback_enabled": fallback_enabled, "spare_capacity_only": spare_capacity_only},
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["user"]["id"]

        key = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": f"{name}-key"})
        assert key.status_code == 201, key.text
        return key.json()["secret"]

    return _make
