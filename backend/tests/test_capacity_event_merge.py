"""Rule data migration, legacy cooldown continuity, and unified alert coverage."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.utils import notifications
from app.utils.notification_migrations import CANONICAL, LEGACY_DEFAULT_TEMPLATES, LEGACY_EVENTS, migrate_capacity_rules
from app.utils.postgres import AccountDb, NotificationChannelDb, NotificationDeliveryDb, NotificationRuleDb, ProxyEventDb
from app.utils.postgres.base import SessionFactory


def channel(db):
    now = datetime.now(timezone.utc)
    result = NotificationChannelDb(id=uuid4(), channel_type="telegram", name="test", enabled=True, created_at=now, updated_at=now)
    db.add(result)
    db.flush()
    return result


@pytest.mark.parametrize("pool_enabled,error_enabled", [(True, True), (True, False), (False, True), (False, False)])
def test_merge_retains_opt_in_longest_cooldown_custom_templates_and_sent_history(pool_enabled, error_enabled):
    now = datetime.now(timezone.utc)
    with SessionFactory() as db:
        cid = channel(db).id
        for kind, enabled, cooldown, template in [
            ("pool_unavailable", pool_enabled, 900, "Pool: {{ usable_accounts }}/{{ total_accounts }}"),
            ("elevated_503", error_enabled, 1800, "Failures: {{ error_count }}"),
        ]:
            db.add(
                NotificationRuleDb(
                    id=uuid4(),
                    channel_id=cid,
                    event_type=kind,
                    enabled=enabled,
                    template=template,
                    cooldown_seconds=cooldown,
                    created_at=now,
                    updated_at=now,
                )
            )
        for status in ("pending", "failed", "sent"):
            db.add(
                NotificationDeliveryDb(
                    id=uuid4(),
                    channel_id=cid,
                    event_type="pool_unavailable",
                    event_key=status,
                    message="old",
                    status=status,
                    attempts=0,
                    available_at=now,
                    created_at=now,
                )
            )
        db.flush()
        assert migrate_capacity_rules(db) == 1
        merged = db.query(NotificationRuleDb).filter_by(event_type=CANONICAL).one()
        assert merged.enabled == (pool_enabled or error_enabled)
        assert merged.cooldown_seconds == (900 if pool_enabled and not error_enabled else 1800)
        assert "Pool:" in merged.template and "Failures:" in merged.template
        notifications.validate_template(CANONICAL, merged.template)
        assert db.query(NotificationRuleDb).filter(NotificationRuleDb.event_type.in_(LEGACY_EVENTS)).count() == 0
        assert db.query(NotificationDeliveryDb).filter_by(status="suppressed").count() == 2
        assert db.query(NotificationDeliveryDb).filter_by(status="sent").count() == 1
        merged.enabled = False
        merged.cooldown_seconds = 1234
        db.flush()
        assert migrate_capacity_rules(db) == 0
        assert not merged.enabled and merged.cooldown_seconds == 1234


def test_known_default_template_upgrades_and_old_sent_alert_preserves_cooldown():
    now = notifications.utcnow()
    with SessionFactory() as db:
        cid = channel(db).id
        db.add(
            NotificationRuleDb(
                id=uuid4(),
                channel_id=cid,
                event_type="pool_unavailable",
                enabled=True,
                template=LEGACY_DEFAULT_TEMPLATES[0],
                cooldown_seconds=900,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            NotificationDeliveryDb(
                id=uuid4(),
                channel_id=cid,
                event_type="elevated_503",
                event_key="503:old",
                message="sent",
                status="sent",
                attempts=1,
                available_at=now,
                created_at=now,
            )
        )
        db.add(ProxyEventDb(id=uuid4(), request_id="req_current", event_type="request.exhausted", status_code=503, created_at=now))
        db.flush()
        assert notifications.enqueue_capacity_exhausted(db, model="test-model", request_id="req_current") == 0
        rule = db.query(NotificationRuleDb).filter_by(event_type=CANONICAL).one()
        assert rule.template == notifications.EVENTS[CANONICAL].default_template
        assert "model" in notifications.EVENTS[CANONICAL].variables
        assert "pool_unavailable" not in notifications.EVENTS and "elevated_503" not in notifications.EVENTS


def test_zero_cooldown_is_per_request_and_message_contains_pool_model_and_useful_reset(seed_account):
    now = notifications.utcnow()
    aid = seed_account("blocked")
    with SessionFactory() as db:
        channel(db)
        notifications.ensure_default_rules(db, db.query(NotificationChannelDb).one())
        rule = db.query(NotificationRuleDb).filter_by(event_type=CANONICAL).one()
        rule.cooldown_seconds = 0
        account = db.get(AccountDb, aid)
        if hasattr(account, "five_hour_used_pct"):
            account.five_hour_used_pct = 1
            account.five_hour_reset_at = now + timedelta(hours=1)
        else:
            account.session_used_pct = 1
            account.session_reset_at = now + timedelta(hours=1)
        account.weekly_used_pct = 1
        account.weekly_reset_at = now + timedelta(days=3)
        db.add(ProxyEventDb(id=uuid4(), request_id="req_first", event_type="request.exhausted", status_code=503, created_at=now))
        db.flush()
        assert notifications.enqueue_capacity_exhausted(db, model="wanted-model", request_id="req_first") == 1
        assert notifications.enqueue_capacity_exhausted(db, model="wanted-model", request_id="req_first") == 0
        db.add(ProxyEventDb(id=uuid4(), request_id="req_second", event_type="request.exhausted", status_code=503, created_at=now))
        db.flush()
        assert notifications.enqueue_capacity_exhausted(db, model="wanted-model", request_id="req_second") == 1
        messages = [d.message for d in db.query(NotificationDeliveryDb).filter_by(event_type=CANONICAL)]
        assert len(messages) == 2
        assert all("wanted-model" in msg and "HTTP 503" in msg and "No eligible" in msg for msg in messages)
        assert all(notifications._format_time(now + timedelta(days=3)) in msg for msg in messages)
