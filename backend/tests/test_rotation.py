# Path: tests/test_rotation.py
# Description: Unit tests for account availability, ordered priority selection, and exhaustion.

import uuid
from datetime import datetime, timedelta, timezone

from app import config
from app.utils import rotation
from app.utils.models.api import AccountStatus
from app.utils.postgres import AccountDb, UserDb
from app.utils.postgres.base import SessionFactory


def _new_user(db):
    user = UserDb(
        id=uuid.uuid4(),
        name="u",
        active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    return user


def test_is_available_gates_on_status_and_threshold(seed_account):
    seed_account("active", session_used_pct=0.10)
    seed_account("disabled", status=AccountStatus.DISABLED)
    seed_account("full", session_used_pct=1.0)
    with SessionFactory() as db:
        by_label = {a.label: a for a in db.query(AccountDb).all()}
        assert rotation.is_available(by_label["active"]) is True
        assert rotation.is_available(by_label["disabled"]) is False
        assert rotation.is_available(by_label["full"]) is False  # >= its rotation_threshold


def test_expired_cooldown_is_normalized(seed_account):
    account_id = seed_account("expired", status=AccountStatus.COOLDOWN)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.cooldown_until = datetime(2020, 1, 1, tzinfo=timezone.utc)
        now = datetime(2020, 1, 2, tzinfo=timezone.utc)
        assert rotation.normalize_expired_cooldown(account, now) is True
        assert account.status == AccountStatus.ACTIVE
        assert account.cooldown_until is None
        assert rotation.normalize_expired_cooldown(account, now) is False


def test_new_account_gets_default_rotation_policy(seed_account):
    acct_id = seed_account("fresh")  # no rotation_threshold passed -> column defaults apply
    with SessionFactory() as db:
        acct = db.get(AccountDb, acct_id)
        assert config.DEFAULT_ROTATION_THRESHOLD == 1.0
        assert acct.five_hour_rotation_threshold == config.DEFAULT_FIVE_HOUR_ROTATION_THRESHOLD
        assert acct.weekly_rotation_threshold == config.DEFAULT_WEEKLY_ROTATION_THRESHOLD
        assert acct.cooldown_seconds == config.DEFAULT_COOLDOWN_SECONDS
        assert acct.max_failover_attempts == config.DEFAULT_MAX_FAILOVER_ATTEMPTS


def test_rejected_token_refreshes_without_a_synthetic_expiry(seed_account, monkeypatch):
    """A provider 401 must refresh the current row without clobbering its expiry."""
    from app.utils import oauth

    account_id = seed_account("force-refresh", expires_in_hours=1)
    refreshed_expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(
        oauth,
        "refresh_access_token",
        lambda _refresh: {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_at": refreshed_expiry},
    )

    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        assert rotation.ensure_fresh_token(db, account, rejected_token="upstream-access-token") == "rotated-access"
        persisted = db.get(AccountDb, account_id)
        assert persisted.expires_at == refreshed_expiry


def _rotate_in_other_session(account_id, access_token: str, refresh_token: str, expires_at: datetime) -> None:
    from app.utils import crypto

    with SessionFactory() as other:
        row = other.get(AccountDb, account_id)
        row.access_token_enc = crypto.encrypt(access_token)
        row.refresh_token_enc = crypto.encrypt(refresh_token)
        row.expires_at = expires_at
        other.commit()


def test_expiring_token_reuses_a_concurrent_rotation(seed_account, monkeypatch):
    """A worker holding a stale row must not spend a refresh token another worker already rotated."""
    from app.utils import oauth

    account_id = seed_account("concurrent-expiry", expires_in_hours=0)
    calls = []
    monkeypatch.setattr(oauth, "refresh_access_token", lambda refresh: calls.append(refresh))

    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        assert account.expires_at is not None  # loaded before the other worker rotates
        _rotate_in_other_session(account_id, "other-access", "other-refresh", datetime.now(timezone.utc) + timedelta(hours=8))
        assert rotation.ensure_fresh_token(db, account) == "other-access"
    assert calls == []


def test_rejected_token_reuses_a_concurrent_rotation(seed_account, monkeypatch):
    """Concurrent 401s on one token trigger one refresh; later callers get the replacement."""
    from app.utils import oauth

    account_id = seed_account("concurrent-401", expires_in_hours=1)
    calls = []
    monkeypatch.setattr(oauth, "refresh_access_token", lambda refresh: calls.append(refresh))

    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        assert account.expires_at is not None
        _rotate_in_other_session(account_id, "other-access", "other-refresh", datetime.now(timezone.utc) + timedelta(hours=8))
        assert rotation.ensure_fresh_token(db, account, rejected_token="upstream-access-token") == "other-access"
    assert calls == []


def test_refresh_spends_the_latest_refresh_token(seed_account, monkeypatch):
    from app.utils import oauth

    account_id = seed_account("latest-refresh", expires_in_hours=0)
    spent = []

    def refresh(refresh_token):
        spent.append(refresh_token)
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_at": datetime.now(timezone.utc) + timedelta(hours=8)}

    monkeypatch.setattr(oauth, "refresh_access_token", refresh)

    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        assert account.expires_at is not None
        # Another worker rotated, but its new access token is already near expiry, so this worker must refresh again.
        _rotate_in_other_session(account_id, "other-access", "other-refresh", datetime.now(timezone.utc))
        assert rotation.ensure_fresh_token(db, account) == "new-access"
    assert spent == ["other-refresh"]


def test_is_available_uses_per_account_threshold(seed_account):
    # The same 0.50 utilization is benched under a 0.40 per-account threshold but available under the default 1.0.
    strict = seed_account("strict", session_used_pct=0.50, rotation_threshold=0.40)
    lenient = seed_account("lenient", session_used_pct=0.50)
    with SessionFactory() as db:
        by_id = {a.id: a for a in db.query(AccountDb).all()}
        assert rotation.is_available(by_id[strict]) is False
        assert rotation.is_available(by_id[lenient]) is True


def test_each_quota_window_uses_its_own_rotation_threshold(seed_account):
    seed_account(
        "five-hour-strict",
        session_used_pct=0.80,
        weekly_used_pct=0.20,
        five_hour_rotation_threshold=0.75,
        weekly_rotation_threshold=1.0,
    )
    seed_account(
        "weekly-strict",
        session_used_pct=0.20,
        weekly_used_pct=0.80,
        five_hour_rotation_threshold=1.0,
        weekly_rotation_threshold=0.75,
    )
    with SessionFactory() as db:
        accounts = {account.label: account for account in db.query(AccountDb).all()}
        assert rotation.is_available(accounts["five-hour-strict"]) is False
        assert rotation.is_available(accounts["weekly-strict"]) is False


def test_preview_account_uses_priority_not_load(seed_account):
    later_id = seed_account("later", session_used_pct=0.10, priority=2)
    first_id = seed_account("first", session_used_pct=0.50, priority=1)
    with SessionFactory() as db:
        assert rotation.preview_account(db, _new_user(db)).id == first_id
        assert rotation.preview_account(db, exclude_ids={first_id}).id == later_id


def test_same_priority_prefers_earliest_weekly_reset_then_five_hour(seed_account):
    weekly_later = seed_account("weekly-later", priority=1)
    weekly_sooner = seed_account("weekly-sooner", priority=1)
    seed_account("five-hour-later", priority=2)
    five_hour_sooner = seed_account("five-hour-sooner", priority=2)
    now = datetime.now(timezone.utc)
    with SessionFactory() as db:
        accounts = {account.label: account for account in db.query(AccountDb).all()}
        accounts["weekly-later"].weekly_reset_at = now + timedelta(days=7)
        accounts["weekly-sooner"].weekly_reset_at = now + timedelta(days=2)
        accounts["weekly-later"].session_reset_at = now + timedelta(hours=1)
        accounts["weekly-sooner"].session_reset_at = now + timedelta(hours=4)
        accounts["five-hour-later"].session_reset_at = now + timedelta(hours=4)
        accounts["five-hour-sooner"].session_reset_at = now + timedelta(hours=1)
        db.commit()
        assert rotation.preview_account(db).id == weekly_sooner
        assert rotation.preview_account(db, exclude_ids={weekly_later, weekly_sooner}).id == five_hour_sooner


def test_select_account_then_exhausts(seed_account):
    only_id = seed_account("only", session_used_pct=0.10)
    with SessionFactory() as db:
        user = _new_user(db)
        chosen = rotation.select_account(db, user)
        assert chosen.id == only_id

        # Excluding the only available account leaves nothing to serve.
        assert rotation.select_account(db, user, exclude_ids={only_id}) is None


def test_usage_probe_clears_rolling_cold_five_hour_placeholder(seed_account):
    account_id = seed_account("cold", session_used_pct=0.10)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.session_reset_at = datetime(2030, 1, 1, tzinfo=timezone.utc)

        rotation.apply_usage_probe(
            account,
            {
                "five_hour": {
                    "utilization": 0.0,
                    "reset_at": datetime(2030, 1, 2, tzinfo=timezone.utc),
                    "window_seconds": 18_000,
                    "reset_after_seconds": 18_000,
                    "is_cold": True,
                },
                "seven_day": None,
            },
        )

        assert account.session_used_pct == 0.0
        assert account.session_reset_at is None


def test_weekly_exhausted_account_yields_to_remaining_pool(seed_account):
    seed_account("weekly-full", weekly_used_pct=1.0)
    remaining = seed_account("remaining", weekly_used_pct=0.4)
    with SessionFactory() as db:
        assert rotation.select_account(db).id == remaining
