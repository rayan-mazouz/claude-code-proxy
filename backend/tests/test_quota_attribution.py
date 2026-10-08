# Path: tests/test_quota_attribution.py
# Description: Each account's 5-hour, weekly and monthly usage growth is credited to the users who caused it.

import json
import uuid
from datetime import datetime, timedelta, timezone

from app.utils import quota_attribution
from app.utils.postgres import AccountDb, UsageRecordDb, UserDb
from app.utils.postgres.base import SessionFactory

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _user_id(name):
    with SessionFactory() as db:
        return db.query(UserDb).filter(UserDb.name == name).one().id


def _update_account(account_id, **fields):
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        for field, value in fields.items():
            setattr(account, field, value)
        db.commit()


def _cost(user_name, account_id, cost, at):
    with SessionFactory() as db:
        db.add(
            UsageRecordDb(
                id=uuid.uuid4(),
                user_id=_user_id(user_name),
                account_id=account_id,
                model="claude-haiku-4-5",
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                billed_cost_usd=cost,
                created_at=at,
            )
        )
        db.commit()


def _observe(account_id, now):
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        quota_attribution.observe(db, account, now)
        db.commit()
        return json.loads(account.quota_attribution_json)


def _shares(*names, now=NOW):
    with SessionFactory() as db:
        shares = quota_attribution.user_shares(db, [_user_id(name) for name in names], now)
    return {name: shares[str(_user_id(name))] for name in names}


def test_first_probe_credits_the_window_so_far_by_cost(seed_account, make_user):
    account_id = seed_account("first", session_used_pct=0.4)
    _update_account(account_id, session_reset_at=NOW + timedelta(hours=3))
    make_user("alice")
    make_user("bob")
    _cost("alice", account_id, 3.0, NOW - timedelta(hours=1))
    _cost("bob", account_id, 1.0, NOW - timedelta(minutes=30))
    # Before the window opened: not part of it.
    _cost("bob", account_id, 50.0, NOW - timedelta(hours=3))

    users = _observe(account_id, NOW)["five_hour"]["users"]
    assert round(users[str(_user_id("alice"))], 6) == 0.3
    assert round(users[str(_user_id("bob"))], 6) == 0.1


def test_growth_without_proxy_traffic_is_credited_to_nobody(seed_account, make_user):
    account_id = seed_account("outside", session_used_pct=0.2)
    _update_account(account_id, session_reset_at=NOW + timedelta(hours=3))
    make_user("carol")
    _cost("carol", account_id, 1.0, NOW - timedelta(minutes=30))
    _observe(account_id, NOW)

    # Someone used the account outside the proxy: 20% -> 50% with no proxy requests.
    _update_account(account_id, session_used_pct=0.5)
    entry = _observe(account_id, NOW + timedelta(minutes=1))["five_hour"]
    assert entry["last_used"] == 0.5
    assert round(entry["users"][str(_user_id("carol"))], 6) == 0.2


def test_growth_is_split_by_the_interval_cost(seed_account, make_user):
    account_id = seed_account("interval", weekly_used_pct=0.1)
    _update_account(account_id, weekly_reset_at=NOW + timedelta(days=3))
    make_user("dave")
    make_user("erin")
    _cost("dave", account_id, 1.0, NOW - timedelta(days=1))
    _observe(account_id, NOW)

    _cost("erin", account_id, 1.0, NOW + timedelta(seconds=30))
    _update_account(account_id, weekly_used_pct=0.15)
    users = _observe(account_id, NOW + timedelta(minutes=1))["weekly"]["users"]
    assert round(users[str(_user_id("dave"))], 6) == 0.1
    assert round(users[str(_user_id("erin"))], 6) == 0.05


def test_a_new_window_starts_from_zero(seed_account, make_user):
    account_id = seed_account("rolled", session_used_pct=0.9)
    _update_account(account_id, session_reset_at=NOW + timedelta(minutes=10))
    make_user("frank")
    _cost("frank", account_id, 1.0, NOW - timedelta(hours=1))
    _observe(account_id, NOW)

    _update_account(account_id, session_used_pct=0.05, session_reset_at=NOW + timedelta(hours=4, minutes=50))
    entry = _observe(account_id, NOW + timedelta(minutes=10))["five_hour"]
    assert entry["users"] == {}


def test_monthly_window_opens_one_calendar_month_before_its_reset():
    assert quota_attribution._window_start("monthly", datetime(2026, 3, 31, tzinfo=timezone.utc)) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert quota_attribution._window_start("monthly", datetime(2026, 1, 15, tzinfo=timezone.utc)) == datetime(2025, 12, 15, tzinfo=timezone.utc)


def test_user_shares_sum_across_accounts(seed_account, make_user):
    first = seed_account("sum-a", weekly_used_pct=0.2)
    second = seed_account("sum-b", weekly_used_pct=0.4)
    _update_account(first, weekly_reset_at=NOW + timedelta(days=2))
    _update_account(second, weekly_reset_at=NOW + timedelta(days=5))
    make_user("gina")
    make_user("idle")
    _cost("gina", first, 1.0, NOW - timedelta(hours=1))
    _cost("gina", second, 1.0, NOW - timedelta(hours=1))
    _observe(first, NOW)
    _observe(second, NOW)

    shares = _shares("gina", "idle")
    assert round(shares["gina"]["weekly"].used_pct, 6) == 0.6
    assert shares["gina"]["weekly"].reset_at == NOW + timedelta(days=2)
    assert shares["idle"]["weekly"].used_pct == 0.0
    # No account has a monthly window open.
    assert shares["gina"]["monthly"] is None


def test_users_endpoint_reports_quota_usage(client, admin_headers, seed_account, make_user):
    account_id = seed_account("listed", session_used_pct=0.25)
    now = datetime.now(timezone.utc)
    _update_account(account_id, session_reset_at=now + timedelta(hours=2))
    make_user("henry")
    _cost("henry", account_id, 1.0, now - timedelta(minutes=5))
    _observe(account_id, now)

    listed = client.get("/api/v1/users", headers=admin_headers).json()["users"]
    henry = next(user for user in listed if user["name"] == "henry")
    assert henry["quota_usage"]["five_hour"]["used_pct"] == 0.25
    assert henry["quota_usage"]["monthly"] is None
