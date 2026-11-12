"""Idempotent data migration for the two formerly overlapping capacity rules."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.utils.postgres import NotificationDeliveryDb, NotificationRuleDb

LEGACY_EVENTS = ("pool_unavailable", "elevated_503")
LEGACY_DEFAULT_TEMPLATES = (
    (
        "🛑 Claude account pool unavailable\n"
        "\n"
        "No pooled account can currently serve requests.\n"
        "Total accounts: {{ total_accounts }}\n"
        "Usable accounts: {{ usable_accounts }}\n"
        "Next known reset: {{ next_reset_at }}\n"
        "Detected: {{ event_time }}"
    ),
    (
        "🚨 Proxy request exhausted (HTTP 503)\n"
        "\n"
        "No subscription account or fallback could serve a request.\n"
        "503 responses in the last {{ window_seconds }} seconds: {{ error_count }}\n"
        "Affected users: {{ affected_user_count }}\n"
        "Window: {{ window_seconds }} seconds\n"
        "Users: {{ affected_users }}\n"
        "Detected: {{ event_time }}"
    ),
    (
        "🚨 Elevated 503 errors detected\n"
        "\n"
        "Multiple users are experiencing unavailable proxy capacity.\n"
        "503 responses: {{ error_count }}\n"
        "Affected users: {{ affected_user_count }}\n"
        "Window: {{ window_seconds }} seconds\n"
        "Users: {{ affected_users }}\n"
        "Detected: {{ event_time }}"
    ),
)

CANONICAL = "request_capacity_exhausted"


def migrate_capacity_rules(db, channel_id=None):
    query = db.query(NotificationRuleDb).filter(NotificationRuleDb.event_type.in_(LEGACY_EVENTS))
    if channel_id is not None:
        query = query.filter(NotificationRuleDb.channel_id == channel_id)
    if query.first() is None:
        return 0
    db.execute(text("SELECT pg_advisory_xact_lock(837293758)"))
    legacy = query.populate_existing().all()
    from app.utils.notifications import EVENTS

    definition = EVENTS[CANONICAL]
    count = 0
    for cid in {row.channel_id for row in legacy}:
        rules = sorted([row for row in legacy if row.channel_id == cid], key=lambda row: (not row.enabled, row.event_type))
        current = db.query(NotificationRuleDb).filter_by(channel_id=cid, event_type=CANONICAL).first()
        if current is None:
            custom = list(dict.fromkeys(row.template for row in rules if row.template not in LEGACY_DEFAULT_TEMPLATES))
            enabled = [row for row in rules if row.enabled]
            current = NotificationRuleDb(
                id=uuid.uuid4(),
                channel_id=cid,
                event_type=CANONICAL,
                enabled=bool(enabled),
                template="\n\n".join(custom) if custom else definition.default_template,
                cooldown_seconds=max(row.cooldown_seconds for row in (enabled or rules)),
                created_at=min(row.created_at for row in rules),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(current)
        db.query(NotificationDeliveryDb).filter(
            NotificationDeliveryDb.channel_id == cid,
            NotificationDeliveryDb.event_type.in_(LEGACY_EVENTS),
            NotificationDeliveryDb.status.in_(("pending", "failed")),
        ).update(
            {NotificationDeliveryDb.status: "suppressed", NotificationDeliveryDb.last_error: "Merged into request_capacity_exhausted"},
            synchronize_session=False,
        )
        for row in rules:
            db.delete(row)
        count += 1
    db.flush()
    return count
