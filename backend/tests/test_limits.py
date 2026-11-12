# Path: tests/test_limits.py
# Description: Plans without a 5-hour window, model-scoped weekly allowances, per-key quota attribution, configurable
#              thinking levels, and the limit report.

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.config import get_settings
from app.utils import oauth, quota_attribution, rotation, spare_capacity, warmup
from app.utils.postgres import AccountDb, ApiKeyDb, UsageRecordDb, UserDb
from app.utils.postgres.base import SessionFactory

ANTHROPIC_MESSAGES = "https://api.anthropic.com/v1/messages"
FUTURE = datetime.now(timezone.utc) + timedelta(days=3)

MAX_PLAN_PROBE = {
    "five_hour": None,
    "seven_day": {"utilization": 5.0, "resets_at": FUTURE.isoformat()},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 5, "resets_at": FUTURE.isoformat(), "scope": None, "is_active": True},
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "resets_at": FUTURE.isoformat(),
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": True,
        },
    ],
}


@respx.mock
def test_probe_reports_missing_five_hour_window_and_scoped_limits():
    respx.get(oauth.config.OAUTH_USAGE_URL).mock(return_value=httpx.Response(200, json=MAX_PLAN_PROBE))

    usage = oauth.fetch_usage("access-token")

    assert usage["five_hour"] is None
    assert usage["has_five_hour_limit"] is False
    assert usage["seven_day"]["utilization"] == pytest.approx(0.05)
    assert [limit["model"] for limit in usage["scoped_limits"]] == ["Fable"]
    assert usage["scoped_limits"][0]["used_pct"] == pytest.approx(1.0)


@respx.mock
def test_probe_without_weekly_bucket_does_not_claim_the_five_hour_window_is_gone():
    respx.get(oauth.config.OAUTH_USAGE_URL).mock(return_value=httpx.Response(200, json={"five_hour": None}))

    assert "has_five_hour_limit" not in oauth.fetch_usage("access-token")


def _max_plan_account(seed_account, label="max", **fields):
    account_id = seed_account(label, session_used_pct=1.0)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        # A stale 5-hour reading left over from before the plan dropped the window.
        account.session_reset_at = datetime.now(timezone.utc) - timedelta(days=4)
        rotation.apply_usage_probe(
            account,
            {
                "five_hour": None,
                "seven_day": {"utilization": 0.05, "reset_at": FUTURE},
                "has_five_hour_limit": False,
                "scoped_limits": [{"model": "Fable", "used_pct": 1.0, "reset_at": FUTURE, "active": True}],
            },
        )
        for field, value in fields.items():
            setattr(account, field, value)
        db.commit()
    return account_id


def test_account_without_five_hour_window_is_not_blocked_by_stale_reading(seed_account):
    account_id = _max_plan_account(seed_account)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        assert account.has_five_hour_limit is False
        assert account.session_used_pct is None and account.session_reset_at is None
        assert [window.key for window in rotation.quota_windows(account)] == ["weekly", "monthly"]
        assert [window.key for window in spare_capacity.quota_windows(account)] == ["weekly"]
        assert rotation.is_available(account)
        # Warm-up only staggers 5-hour windows.
        assert not warmup.is_eligible(account)

        # A zero 5-hour reading in the headers is no evidence of a 5-hour window.
        rotation.update_quota_from_headers(account, {"anthropic-ratelimit-unified-5h-utilization": "0"})
        assert account.has_five_hour_limit is False
        assert account.session_used_pct is None

        # A short hard limit (per-minute, concurrency) is left to the cooldown and does not use up the week.
        body = json.dumps({"error": {"type": "rate_limit_error", "resets_in_seconds": 60}}).encode()
        assert rotation.apply_rate_limit_body(account, body)[2] is None
        assert account.session_used_pct is None
        assert account.weekly_used_pct == pytest.approx(0.05)

        # A reset days away can only be the weekly limit.
        body = json.dumps({"error": {"type": "rate_limit_error", "resets_in_seconds": 2 * 86400}}).encode()
        rotation.apply_rate_limit_body(account, body)
        assert account.weekly_used_pct == 1.0


def test_five_hour_headers_never_flip_the_probe_decision(seed_account):
    account_id = _max_plan_account(seed_account)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        rotation.update_quota_from_headers(account, {"anthropic-ratelimit-unified-5h-utilization": "0.25"})
        assert account.has_five_hour_limit is False
        assert account.session_used_pct is None


def test_hard_limit_matching_a_scoped_allowance_only_marks_that_allowance(seed_account):
    account_id = seed_account("pro")
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.scoped_limits_json = json.dumps([{"model": "Fable 5.2", "used_pct": 0.95, "reset_at": FUTURE.isoformat(), "active": True}])
        body = json.dumps({"error": {"type": "rate_limit_error", "resets_at": FUTURE.isoformat()}}).encode()

        reached, _, scoped = rotation.apply_rate_limit_body(account, body, "claude-fable-5-2")

        assert reached == "rate_limit_error"
        assert scoped is not None and scoped.model == "Fable 5.2"
        assert account.session_used_pct == 0.0  # the account itself stays usable
        assert rotation.scoped_limit_exhausted(account, "claude-fable-5-2")
        assert not rotation.scoped_limit_exhausted(account, "claude-opus-5-5")
        # Other models' hard limits still mark the account.
        rotation.apply_rate_limit_body(account, body, "claude-opus-5-5")
        assert account.session_used_pct == 1.0


def test_ambiguous_hard_limits_stay_account_wide(seed_account):
    account_id = seed_account("pro", weekly_used_pct=0.5)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.scoped_limits_json = json.dumps([{"model": "Fable", "used_pct": 0.95, "reset_at": FUTURE.isoformat(), "active": True}])
        short = json.dumps({"error": {"type": "rate_limit_error", "resets_in_seconds": 30}}).encode()
        missing = json.dumps({"error": {"type": "rate_limit_error"}}).encode()
        assert rotation.apply_rate_limit_body(account, short, "claude-fable-5-2")[2] is None
        assert rotation.apply_rate_limit_body(account, missing, "claude-fable-5-2")[2] is None
        assert not rotation.scoped_limit_exhausted(account, "claude-fable-5-2")
        # With the weekly window itself at its threshold the 429 may be account-wide.
        account.session_used_pct = 0.1
        account.weekly_used_pct = 1.0
        weekly = json.dumps({"error": {"type": "rate_limit_error", "resets_at": FUTURE.isoformat()}}).encode()
        assert rotation.apply_rate_limit_body(account, weekly, "claude-fable-5-2")[2] is None


@respx.mock
def test_scoped_allowance_429_skips_the_account_only_for_that_request(client, make_user, seed_account):
    scoped_id = seed_account("scoped", priority=1)
    seed_account("other", priority=2)
    with SessionFactory() as db:
        db.get(AccountDb, scoped_id).scoped_limits_json = json.dumps(
            [{"model": "Fable", "used_pct": 0.95, "reset_at": FUTURE.isoformat(), "active": True}]
        )
        db.commit()
    key = make_user("fable-user")
    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        side_effect=[
            httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "resets_at": FUTURE.isoformat()}}),
            httpx.Response(200, json={"model": "claude-fable-5-2", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )

    response = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-fable-5-2"})

    assert response.status_code == 200, response.text
    with SessionFactory() as db:
        account = db.get(AccountDb, scoped_id)
        assert account.cooldown_until is None
        assert rotation.is_available(account)
        assert rotation.scoped_limit_exhausted(account, "claude-fable-5-2")


def test_probe_restores_the_five_hour_window_when_it_comes_back(seed_account):
    account_id = _max_plan_account(seed_account)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        rotation.apply_usage_probe(
            account,
            {
                "five_hour": {"utilization": 0.3, "reset_at": FUTURE},
                "seven_day": {"utilization": 0.1, "reset_at": FUTURE},
                "has_five_hour_limit": True,
            },
        )
        assert account.has_five_hour_limit is True
        assert account.session_used_pct == 0.3


def test_exhausted_scoped_allowance_only_skips_that_model_family(seed_account):
    max_id = _max_plan_account(seed_account, "max", priority=1)
    other_id = seed_account("other", priority=2)
    with SessionFactory() as db:
        assert rotation.select_account(db, model="claude-opus-5-5").id == max_id
        assert rotation.select_account(db, model="claude-fable-5-2").id == other_id
        assert rotation.select_account(db, model="claude-fable-5-2[1m]").id == other_id
        account = db.get(AccountDb, max_id)
        assert rotation.recovery_time(account, model="claude-fable-5-2") is not None
        assert rotation.recovery_time(account, model="claude-fable-5-2") >= FUTURE - timedelta(seconds=1)


def test_scoped_allowance_below_threshold_or_reset_still_serves(seed_account):
    account_id = _max_plan_account(seed_account)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        account.scoped_limits_json = json.dumps([{"model": "Fable", "used_pct": 1.0, "reset_at": past, "active": True}])
        assert not rotation.scoped_limit_exhausted(account, "claude-fable-5-2")
        account.scoped_limits_json = json.dumps([{"model": "Fable", "used_pct": 0.5, "reset_at": FUTURE.isoformat(), "active": True}])
        assert not rotation.scoped_limit_exhausted(account, "claude-fable-5-2")
        account.weekly_rotation_threshold = 0.5
        assert rotation.scoped_limit_exhausted(account, "claude-fable-5-2")
        # The family name must match a whole segment of the model ID.
        account.scoped_limits_json = json.dumps([{"model": "Opus", "used_pct": 1.0, "reset_at": FUTURE.isoformat(), "active": True}])
        assert not rotation.scoped_limit_exhausted(account, "claude-fable-5-2")
        assert rotation.scoped_limit_exhausted(account, "claude-opus-5-5")


def test_overview_and_pool_status_ignore_accounts_without_five_hour_window(client, admin_headers, seed_account):
    _max_plan_account(seed_account, "max")
    seed_account("pro", session_used_pct=0.4)

    overview = client.get("/api/v1/stats/overview", headers=admin_headers).json()
    assert overview["five_hour_limited_accounts"] == 1
    assert overview["five_hour_average_pct"] == pytest.approx(40.0)

    from app.routes.me import build_pool_status

    with SessionFactory() as db:
        pool = build_pool_status(db)
    assert pool.five_hour.used_pct == pytest.approx(0.4)
    assert pool.five_hour.known_account_count == 1

    accounts = {item["label"]: item for item in client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"]}
    assert accounts["max"]["has_five_hour_limit"] is False
    assert accounts["max"]["scoped_limits"][0]["model"] == "Fable"
    assert accounts["pro"]["has_five_hour_limit"] is True

    report = client.get("/api/v1/stats/limits", headers=admin_headers).json()
    by_label = {account["label"]: account for account in report["accounts"]}
    assert by_label["max"]["five_hour"] is None
    assert by_label["max"]["scoped"][0]["used_pct"] == 1.0
    assert by_label["pro"]["five_hour"]["used_pct"] == pytest.approx(0.4)


def test_attribution_credits_users_and_keys_per_account(seed_account):
    account_id = seed_account("acct")
    now = datetime.now(timezone.utc)
    with SessionFactory() as db:
        users = [UserDb(id=uuid.uuid4(), name=name, active=True, created_at=now) for name in ("alice", "bob")]
        db.add_all(users)
        db.flush()
        keys = [
            ApiKeyDb(id=uuid.uuid4(), user_id=users[0].id, label="a1", key_prefix="usr_a1", key_hash="h1", active=True, created_at=now),
            ApiKeyDb(id=uuid.uuid4(), user_id=users[0].id, label="a2", key_prefix="usr_a2", key_hash="h2", active=True, created_at=now),
            ApiKeyDb(id=uuid.uuid4(), user_id=users[1].id, label="b1", key_prefix="usr_b1", key_hash="h3", active=True, created_at=now),
        ]
        db.add_all(keys)
        db.flush()
        for key, cost in zip(keys, (1.0, 1.0, 2.0), strict=True):
            db.add(
                UsageRecordDb(
                    id=uuid.uuid4(),
                    user_id=key.user_id,
                    api_key_id=key.id,
                    account_id=account_id,
                    model="claude-haiku-4-5",
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    billed_cost_usd=cost,
                    created_at=now - timedelta(minutes=5),
                )
            )
        db.flush()
        account = db.get(AccountDb, account_id)
        account.weekly_used_pct = 0.4
        account.weekly_reset_at = now + timedelta(days=6)
        quota_attribution.observe(db, account, now)
        db.commit()

        user_totals = quota_attribution.shares(db, "users", [user.id for user in users], now)
        key_totals = quota_attribution.shares(db, "keys", [key.id for key in keys], now)
        by_account = quota_attribution.shares_by_account(db, "keys", [keys[0].id], now)

    assert user_totals[str(users[0].id)]["weekly"].used_pct == pytest.approx(0.2)
    assert user_totals[str(users[1].id)]["weekly"].used_pct == pytest.approx(0.2)
    assert key_totals[str(keys[0].id)]["weekly"].used_pct == pytest.approx(0.1)
    assert key_totals[str(keys[2].id)]["weekly"].used_pct == pytest.approx(0.2)
    assert by_account[str(keys[0].id)][0].label == "acct"
    assert by_account[str(keys[0].id)][0].windows["weekly"].used_pct == pytest.approx(0.1)
    # No 5-hour window was open, so it is reported as absent rather than zero.
    assert by_account[str(keys[0].id)][0].windows["five_hour"] is None


def test_entries_without_keys_are_backfilled_from_the_users_key_costs(seed_account):
    account_id = seed_account("acct")
    now = datetime.now(timezone.utc)
    reset_at = now + timedelta(days=6)
    with SessionFactory() as db:
        user = UserDb(id=uuid.uuid4(), name="legacy", active=True, created_at=now)
        db.add(user)
        db.flush()
        keys = [
            ApiKeyDb(id=uuid.uuid4(), user_id=user.id, label=f"k{i}", key_prefix=f"usr_k{i}", key_hash=f"hk{i}", active=True, created_at=now)
            for i in range(2)
        ]
        db.add_all(keys)
        db.flush()
        for key, cost in zip(keys, (3.0, 1.0), strict=True):
            db.add(
                UsageRecordDb(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    api_key_id=key.id,
                    account_id=account_id,
                    model="claude-haiku-4-5",
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    billed_cost_usd=cost,
                    created_at=now - timedelta(hours=1),
                )
            )
        db.flush()
        account = db.get(AccountDb, account_id)
        account.weekly_used_pct = 0.4
        account.weekly_reset_at = reset_at
        # Written by a release that only tracked users.
        account.quota_attribution_json = json.dumps(
            {"weekly": {"reset_at": reset_at.isoformat(), "observed_at": now.isoformat(), "last_used": 0.4, "users": {str(user.id): 0.4}}}
        )
        quota_attribution.observe(db, account, now + timedelta(seconds=1))
        db.commit()
        totals = quota_attribution.shares(db, "keys", [key.id for key in keys], now)

    assert totals[str(keys[0].id)]["weekly"].used_pct == pytest.approx(0.3)
    assert totals[str(keys[1].id)]["weekly"].used_pct == pytest.approx(0.1)


def test_budget_breach_reports_the_weekly_cap_first(seed_account):
    account_id = seed_account("acct")
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    tracking = {
        key: {"reset_at": (now + delta).isoformat(), "observed_at": now.isoformat(), "last_used": 0.5, "users": {str(user_id): 0.5}, "keys": {}}
        for key, delta in (("five_hour", timedelta(hours=2)), ("weekly", timedelta(days=3)))
    }
    with SessionFactory() as db:
        db.get(AccountDb, account_id).quota_attribution_json = json.dumps(tracking)
        db.commit()
        assert quota_attribution.budget_breach(db, "users", user_id, None, None) is None
        assert quota_attribution.budget_breach(db, "users", user_id, 0.6, 0.6) is None
        assert quota_attribution.budget_breach(db, "users", user_id, 0.5, None).window == "five_hour"
        breach = quota_attribution.budget_breach(db, "users", user_id, 0.5, 0.4)
    assert breach.window == "weekly"
    assert breach.budget == 0.4


@pytest.fixture
def xhigh_enabled(monkeypatch):
    monkeypatch.setenv("THINKING_LEVELS", "low,medium,high,xhigh,max")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("THINKING_LEVELS")
    get_settings.cache_clear()


def test_levels_that_are_no_longer_enabled_are_dropped_on_save(client, admin_headers, monkeypatch):
    created = client.post("/api/v1/users", headers=admin_headers, json={"name": "legacy", "allowed_thinking_levels": ["low", "max"]}).json()["user"]
    monkeypatch.setenv("THINKING_LEVELS", "low,medium,high")
    get_settings.cache_clear()
    try:
        renamed = client.put(
            f"/api/v1/users/{created['id']}",
            headers=admin_headers,
            json={"name": "renamed", "allowed_thinking_levels": ["low", "max"], "model_thinking_levels": {"claude-opus-5-5": ["max", "low"]}},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["user"]["allowed_thinking_levels"] == ["low"]
        assert renamed.json()["user"]["model_thinking_levels"] == {"claude-opus-5-5": ["low"]}
        only_disabled = client.put(f"/api/v1/users/{created['id']}", headers=admin_headers, json={"allowed_thinking_levels": ["max"]})
        assert only_disabled.status_code == 422
    finally:
        monkeypatch.delenv("THINKING_LEVELS")
        get_settings.cache_clear()


def test_unknown_thinking_level_setting_is_rejected_at_load():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="unknown effort level"):
        Settings(_env_file=None, THINKING_LEVELS="low,hihg")


def test_thinking_levels_follow_configuration(client, admin_headers):
    assert client.get("/api/v1/users/model-options", headers=admin_headers).json()["thinking_levels"] == ["low", "medium", "high", "max"]
    rejected = client.post("/api/v1/users", headers=admin_headers, json={"name": "eager", "allowed_thinking_levels": ["xhigh"]})
    assert rejected.status_code == 422


def test_xhigh_can_be_enabled(client, admin_headers, xhigh_enabled):
    assert client.get("/api/v1/users/model-options", headers=admin_headers).json()["thinking_levels"] == ["low", "medium", "high", "xhigh", "max"]
    created = client.post("/api/v1/users", headers=admin_headers, json={"name": "eager", "allowed_thinking_levels": ["xhigh", "low"]})
    assert created.status_code == 201, created.text
    assert created.json()["user"]["allowed_thinking_levels"] == ["low", "xhigh"]


@respx.mock
def test_unconfigured_effort_level_is_refused(client, make_user, seed_account):
    seed_account("a1")
    key = make_user("effort")
    respx.route(host="testserver").pass_through()
    upstream = respx.post(ANTHROPIC_MESSAGES).mock(return_value=httpx.Response(200, json={"usage": {}}))

    response = client.post(
        "/api/v1/messages",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "claude-opus-5-5", "output_config": {"effort": "xhigh"}},
    )

    assert response.status_code == 403
    assert "not enabled on this proxy" in response.text
    assert upstream.call_count == 0


def test_me_usage_reports_limit_budgets(client, admin_headers, seed_account):
    seed_account("a1")
    user_id = client.post("/api/v1/users", headers=admin_headers, json={"name": "self", "weekly_limit_budget": 0.3}).json()["user"]["id"]
    secret = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "k", "five_hour_limit_budget": 0.1}).json()["secret"]

    body = client.get("/api/v1/me/usage", headers={"Authorization": f"Bearer {secret}"}).json()

    assert body["key_limits"]["budget"] == {"five_hour": 0.1, "weekly": None}
    assert body["user_limits"]["budget"] == {"five_hour": None, "weekly": 0.3}
    assert body["monthly_token_budget"] is None
