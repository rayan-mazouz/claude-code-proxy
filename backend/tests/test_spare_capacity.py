# Path: tests/test_spare_capacity.py
# Description: Spare-capacity users only get quota other users are not projected to need, per 5-hour and weekly window.

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.utils import spare_capacity
from app.utils.postgres import AccountDb, ProxyEventDb, UsageRecordDb, UserDb
from app.utils.postgres.base import SessionFactory

ANTHROPIC_MESSAGES = "https://api.anthropic.com/v1/messages"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _window(used, reset_in, length=spare_capacity.FIVE_HOUR_WINDOW, floor=0.1):
    reset_at = None if reset_in is None else NOW + reset_in
    return spare_capacity.QuotaWindow("five_hour", used, reset_at, length, floor)


def test_reserve_is_the_floor_until_other_users_use_the_window():
    assert spare_capacity.reserve(_window(0.5, timedelta(hours=3)), others=0.0, now=NOW) == 0.1


def test_reserve_projects_other_users_pace_over_the_rest_of_the_window():
    # 2h elapsed, 3h left, others used 30%: 30% * 3/2 = 45% more is held back.
    assert round(spare_capacity.reserve(_window(0.4, timedelta(hours=3)), others=0.3, now=NOW), 6) == 0.45


def test_early_burst_by_other_users_reserves_the_rest_of_the_window():
    # 10 minutes in, others used 20%: projected 20% * 290/10 = 580%, far more than the 80% left.
    assert round(spare_capacity.reserve(_window(0.2, timedelta(hours=4, minutes=50)), others=0.2, now=NOW), 6) == 5.8


def test_reserve_is_the_floor_for_cold_or_finished_windows():
    assert spare_capacity.reserve(_window(0.0, None), others=0.5, now=NOW) == 0.1
    assert spare_capacity.reserve(_window(0.7, -timedelta(minutes=1)), others=0.5, now=NOW) == 0.1


def _user_id(name):
    with SessionFactory() as db:
        return db.query(UserDb).filter(UserDb.name == name).one().id


def _update_account(account_id, **fields):
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        for field, value in fields.items():
            setattr(account, field, value)
        db.commit()


def _track(account_id, *, reset_at, observed_at, last_used, others_used, key="five_hour"):
    entry = {"reset_at": reset_at.isoformat(), "observed_at": observed_at.isoformat(), "last_used": last_used, "others_used": others_used}
    _update_account(account_id, spare_capacity_tracking_json=json.dumps({key: entry}))


def _event(account_id, request_id, event_type, at):
    with SessionFactory() as db:
        db.add(ProxyEventDb(id=uuid.uuid4(), request_id=request_id, account_id=account_id, event_type=event_type, created_at=at))
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
        spare_capacity.observe(db, account, now)
        db.commit()
        return json.loads(account.spare_capacity_tracking_json)


def test_usage_outside_the_proxy_counts_as_other_users(seed_account):
    # Nobody else uses the proxy: the 50% used so far is all someone else's, measured from the account itself.
    account_id = seed_account("external", session_used_pct=0.5)
    _update_account(account_id, session_reset_at=NOW + timedelta(hours=3))

    tracking = _observe(account_id, NOW)
    assert tracking["five_hour"]["others_used"] == 0.5
    assert tracking["five_hour"]["last_used"] == 0.5


def test_growth_while_a_spare_request_runs_is_not_counted_as_other_users(seed_account):
    account_id = seed_account("spare-running", session_used_pct=0.4)
    reset_at = NOW + timedelta(hours=3)
    _update_account(account_id, session_reset_at=reset_at)
    _track(account_id, reset_at=reset_at, observed_at=NOW - timedelta(seconds=60), last_used=0.3, others_used=0.2)
    _event(account_id, "req_running", spare_capacity.REQUEST_STARTED, NOW - timedelta(seconds=90))

    assert _observe(account_id, NOW)["five_hour"]["others_used"] == 0.2


def test_growth_while_spare_and_other_proxy_users_run_is_split_by_cost(seed_account, make_user):
    account_id = seed_account("mixed", session_used_pct=0.4)
    reset_at = NOW + timedelta(hours=3)
    _update_account(account_id, session_reset_at=reset_at)
    make_user("mixed-regular")
    make_user("mixed-spare", spare_capacity_only=True)
    _track(account_id, reset_at=reset_at, observed_at=NOW - timedelta(seconds=60), last_used=0.3, others_used=0.2)
    _event(account_id, "req_mixed", spare_capacity.REQUEST_STARTED, NOW - timedelta(seconds=90))
    _cost("mixed-regular", account_id, 3.0, NOW - timedelta(seconds=30))
    _cost("mixed-spare", account_id, 1.0, NOW - timedelta(seconds=20))

    # 10% growth, 75% of the interval's proxy cost was the regular user's.
    assert round(_observe(account_id, NOW)["five_hour"]["others_used"], 6) == 0.275


def test_finished_and_abandoned_spare_requests_do_not_hide_other_users(seed_account):
    account_id = seed_account("idle-spare", session_used_pct=0.4)
    reset_at = NOW + timedelta(hours=3)
    _update_account(account_id, session_reset_at=reset_at)
    _track(account_id, reset_at=reset_at, observed_at=NOW - timedelta(seconds=60), last_used=0.3, others_used=0.2)
    _event(account_id, "req_done", spare_capacity.REQUEST_STARTED, NOW - timedelta(minutes=10))
    _event(account_id, "req_done", spare_capacity.REQUEST_FINISHED, NOW - timedelta(minutes=5))
    _event(account_id, "req_lost", spare_capacity.REQUEST_STARTED, NOW - timedelta(minutes=45))

    assert round(_observe(account_id, NOW)["five_hour"]["others_used"], 6) == 0.3


def test_a_new_window_starts_tracking_from_zero(seed_account):
    account_id = seed_account("rolled", session_used_pct=0.1)
    reset_at = NOW + timedelta(hours=4)
    _update_account(account_id, session_reset_at=reset_at)
    _track(account_id, reset_at=reset_at - timedelta(hours=5), observed_at=NOW - timedelta(hours=1, minutes=1), last_used=0.9, others_used=0.8)

    entry = _observe(account_id, NOW)["five_hour"]
    assert entry["reset_at"] == reset_at.isoformat()
    assert entry["others_used"] == 0.1


def _mock_upstream():
    respx.route(host="testserver").pass_through()
    return respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    )


def _send(client, key):
    return client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})


def _busy_account(seed_account, label, *, priority=None):
    """An account 2h into its 5-hour window at 50%, of which other users caused 37.5% (projected 56.25% more)."""
    account_id = seed_account(label, session_used_pct=0.5, priority=priority)
    now = datetime.now(timezone.utc)
    reset_at = now + timedelta(hours=3)
    _update_account(account_id, session_reset_at=reset_at)
    _track(account_id, reset_at=reset_at, observed_at=now, last_used=0.5, others_used=0.375)
    return account_id


@respx.mock
def test_spare_user_is_throttled_while_other_users_reserve_the_window(client, seed_account, make_user):
    _busy_account(seed_account, "busy")
    regular_key = make_user("regular")
    spare_key = make_user("spare", spare_capacity_only=True)
    route = _mock_upstream()

    throttled = _send(client, spare_key)
    assert throttled.status_code == 429
    assert throttled.headers["retry-after"] == "60"
    assert route.call_count == 0

    assert _send(client, regular_key).status_code == 200
    assert route.call_count == 1


@respx.mock
def test_spare_requests_are_marked_on_the_account_they_run_on(client, seed_account, make_user):
    account_id = seed_account("marked")
    spare_key = make_user("marked-spare", spare_capacity_only=True)
    _mock_upstream()

    assert _send(client, spare_key).status_code == 200
    with SessionFactory() as db:
        events = db.query(ProxyEventDb).filter(ProxyEventDb.event_type.startswith("spare_capacity.")).order_by(ProxyEventDb.created_at).all()
    assert [event.event_type for event in events] == [spare_capacity.REQUEST_STARTED, spare_capacity.REQUEST_FINISHED]
    assert {event.account_id for event in events} == {account_id}
    assert len({event.request_id for event in events}) == 1


@respx.mock
def test_weekly_window_reserves_for_other_users(client, seed_account, make_user):
    account_id = seed_account("weekly", weekly_used_pct=0.6)
    now = datetime.now(timezone.utc)
    reset_at = now + timedelta(days=3)
    _update_account(account_id, weekly_reset_at=reset_at)
    # Others caused all 60% in 4 days, so 45% more is projected for the 3 days left: nothing is spare.
    _track(account_id, reset_at=reset_at, observed_at=now, last_used=0.6, others_used=0.6, key="weekly")
    spare_key = make_user("weekly-spare", spare_capacity_only=True)
    _mock_upstream()

    assert _send(client, spare_key).status_code == 429


@respx.mock
def test_spare_user_is_routed_to_an_account_with_spare_room(client, admin_headers, seed_account, make_user):
    _busy_account(seed_account, "busy-first", priority=1)
    seed_account("idle-second", priority=2)
    spare_key = make_user("guest", spare_capacity_only=True)
    _mock_upstream()

    assert _send(client, spare_key).status_code == 200
    record = client.get("/api/v1/stats/usage", headers=admin_headers).json()["items"][0]
    assert record["user_name"] == "guest"
    assert record["account_label"] == "idle-second"


def test_spare_capacity_setting_is_editable(client, admin_headers, make_user):
    make_user("editable")
    user_id = _user_id("editable")

    updated = client.put(f"/api/v1/users/{user_id}", headers=admin_headers, json={"spare_capacity_only": True})
    assert updated.status_code == 200, updated.text
    assert updated.json()["user"]["spare_capacity_only"] is True
    listed = client.get("/api/v1/users", headers=admin_headers).json()["users"]
    assert next(user for user in listed if user["id"] == str(user_id))["spare_capacity_only"] is True


def test_quota_refresher_records_who_used_each_window(seed_account, monkeypatch):
    from app.scripts import quota_refresher
    from app.utils import oauth, warmup

    account_id = seed_account("refreshed")
    reset_at = datetime.now(timezone.utc) + timedelta(hours=3)
    probe = {"five_hour": {"utilization": 0.5, "reset_at": reset_at, "is_cold": False}, "seven_day": None}
    monkeypatch.setattr(oauth, "fetch_usage", lambda _token, **_kwargs: probe)
    monkeypatch.setattr(oauth, "fetch_profile", lambda _token, **_kwargs: {})
    monkeypatch.setattr(warmup, "warm_pool_if_needed", lambda: 0)

    quota_refresher.refresh_once()

    with SessionFactory() as db:
        tracking = json.loads(db.get(AccountDb, account_id).spare_capacity_tracking_json)
    assert tracking["five_hour"]["others_used"] == 0.5
