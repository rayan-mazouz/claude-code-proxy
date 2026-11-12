import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.utils import rotation
from app.utils.models.api import AccountStatus, ProviderHealth
from app.utils.postgres import AccountDb, ProxyEventDb
from app.utils.postgres.base import SessionFactory


@pytest.mark.parametrize(
    "scenario,recovers",
    [
        ("quota", True),
        ("recent_quota", False),
        ("still_exhausted", False),
        ("partial_probe", False),
        ("transient", False),
        ("missing_event", False),
        ("different_deadline", False),
        ("short_backoff", False),
        ("provider_limited", False),
        ("disabled", False),
        ("reauth", False),
        ("invalid_probe", False),
    ],
)
def test_fresh_quota_probe_only_clears_matching_stale_quota_cooldown(seed_account, scenario, recovers):
    account_id = seed_account("parked", session_used_pct=1.0, weekly_used_pct=1.0)
    now = datetime.now(timezone.utc)
    if scenario != "recent_quota":
        now -= timedelta(minutes=2)
    duration = 60 if scenario == "short_backoff" else 300000
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.status = AccountStatus.DISABLED if scenario == "disabled" else AccountStatus.COOLDOWN
        account.provider_health = ProviderHealth.REAUTH_REQUIRED if scenario == "reauth" else ProviderHealth.HEALTHY
        account.cooldown_until = now + timedelta(seconds=duration + (60 if scenario == "different_deadline" else 0))
        old_deadline = account.cooldown_until
        if scenario != "missing_event":
            db.add(
                ProxyEventDb(
                    id=uuid.uuid4(),
                    request_id="req_test",
                    account_id=account_id,
                    event_type="account.transient_error" if scenario == "transient" else "account.rate_limited",
                    message=f"Cooling down for {duration}s",
                    metadata_json=json.dumps({"failure_class": "account_quota"}),
                    created_at=now,
                )
            )
        db.commit()
        fresh = {
            "five_hour": {"utilization": 0.0},
            "seven_day": {"utilization": 1.0 if scenario == "still_exhausted" else 0.0},
            "limit_reached": scenario == "provider_limited",
        }
        if scenario == "partial_probe":
            fresh.pop("seven_day")
        if scenario == "invalid_probe":
            fresh["five_hour"]["utilization"] = -1
        rotation.apply_usage_probe(account, fresh)
        assert (account.cooldown_until is None) == recovers
        if recovers:
            assert account.status == AccountStatus.ACTIVE
        else:
            assert account.cooldown_until == old_deadline


def test_overview_reset_credit_total_uses_enabled_authenticated_pool(client, admin_headers, seed_account):
    first = seed_account("one")
    second = seed_account("two")
    disabled = seed_account("disabled", status=AccountStatus.DISABLED)
    expired = seed_account("reauth")
    with SessionFactory() as db:
        db.get(AccountDb, expired).provider_health = ProviderHealth.REAUTH_REQUIRED
        if hasattr(AccountDb, "reset_credits_available"):
            for account_id, count in [(first, 2), (second, 3), (disabled, 9), (expired, 7)]:
                db.get(AccountDb, account_id).reset_credits_available = count
        db.commit()
    result = client.get("/api/v1/stats/overview", headers=admin_headers).json()
    assert result["total_reset_credits_available"] is None


@pytest.mark.parametrize("change", ["disable", "deadline"])
def test_quota_probe_does_not_overwrite_concurrent_account_changes(seed_account, change):
    account_id = seed_account("concurrent")
    created = datetime.now(timezone.utc) - timedelta(minutes=2)
    deadline = created + timedelta(days=3)
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.status = AccountStatus.COOLDOWN
        account.cooldown_until = deadline
        db.add(
            ProxyEventDb(
                id=uuid.uuid4(),
                request_id="req_concurrent",
                account_id=account_id,
                event_type="account.rate_limited",
                message="Cooling down for 259200s",
                metadata_json=json.dumps({"failure_class": "account_quota"}),
                created_at=created,
            )
        )
        db.commit()
        db.refresh(account)  # Keep a genuinely stale ORM snapshot in this session.
        # Another transaction modifies the row after our probe loaded it.
        with SessionFactory() as other:
            row = other.get(AccountDb, account_id)
            if change == "disable":
                row.status = AccountStatus.DISABLED
            else:
                row.cooldown_until = deadline + timedelta(hours=1)
            other.commit()
        assert not rotation.recover_confirmed_quota_cooldown(
            account, {"five_hour": {"utilization": 0}, "seven_day": {"utilization": 0}, "limit_reached": False}
        )
        db.commit()
        db.refresh(account)
        if change == "disable":
            assert account.status == AccountStatus.DISABLED
        else:
            assert account.cooldown_until == deadline + timedelta(hours=1)
