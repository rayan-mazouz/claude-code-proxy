"""Dashboard countdowns must represent quota an account can actually consume."""

from datetime import datetime, timedelta, timezone

import pytest

from app.utils.postgres import AccountDb
from app.utils.postgres.base import SessionFactory


@pytest.mark.parametrize(
    "short_pct,weekly_pct,short_minutes,weekly_minutes,expected_short,expected_weekly",
    [
        (0.8, 1.0, 5, 120, None, 120),
        (1.0, 1.0, 120, 5, 120, None),
        (1.0, 1.0, 5, 5, 5, 5),
        (0.4, 0.5, 5, 120, 5, 120),
        (0.0, 0.0, 5, 120, None, None),
        (0.5, 1.0, 5, None, None, None),
        (1.0, 0.5, None, 5, None, None),
        (0.5, 1.0, 120, 5, 120, 5),
    ],
)
def test_only_show_resets_that_restore_usable_quota(
    client, admin_headers, seed_account, short_pct, weekly_pct, short_minutes, weekly_minutes, expected_short, expected_weekly
):
    now = datetime.now(timezone.utc)
    account_id = seed_account("quota", session_used_pct=short_pct, weekly_used_pct=weekly_pct)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.session_reset_at = now + timedelta(minutes=short_minutes) if short_minutes is not None else None
        account.weekly_reset_at = now + timedelta(minutes=weekly_minutes) if weekly_minutes is not None else None
        db.commit()
    result = client.get("/api/v1/stats/overview", headers=admin_headers).json()
    for key, expected in [("five_hour_reset_at", expected_short), ("weekly_reset_at", expected_weekly)]:
        if expected is None:
            assert result[key] is None
        else:
            assert datetime.fromisoformat(result[key].replace("Z", "+00:00")) == now + timedelta(minutes=expected)


def test_skip_blocked_account_choose_next_usable_reset(client, admin_headers, seed_account):
    now = datetime.now(timezone.utc)
    blocked = seed_account("blocked", session_used_pct=0.8, weekly_used_pct=1.0)
    usable = seed_account("usable", session_used_pct=0.5, weekly_used_pct=0.5)
    with SessionFactory() as db:
        db.get(AccountDb, blocked).session_reset_at = now + timedelta(minutes=5)
        db.get(AccountDb, blocked).weekly_reset_at = now + timedelta(days=3)
        db.get(AccountDb, usable).session_reset_at = now + timedelta(minutes=30)
        db.commit()
    result = client.get("/api/v1/stats/overview", headers=admin_headers).json()
    assert datetime.fromisoformat(result["five_hour_reset_at"].replace("Z", "+00:00")) == now + timedelta(minutes=30)
    assert result["five_hour_average_pct"] == 75.0


@pytest.mark.parametrize("blocker", ["monthly", "cooldown", "threshold"])
def test_countdown_respects_additional_routing_blockers(client, admin_headers, seed_account, blocker):
    now = datetime.now(timezone.utc)
    account_id = seed_account("blocked", session_used_pct=0.8, weekly_used_pct=0.8)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.session_reset_at = now + timedelta(minutes=5)
        account.weekly_reset_at = now + timedelta(days=3)
        if blocker == "monthly":
            account.monthly_used_pct = 1.0
        elif blocker == "cooldown":
            account.cooldown_until = now + timedelta(minutes=30)
        else:
            account.weekly_rotation_threshold = 0.7
        db.commit()
    result = client.get("/api/v1/stats/overview", headers=admin_headers).json()
    assert result["five_hour_reset_at"] is None
