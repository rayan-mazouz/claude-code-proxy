# Path: app/scripts/migrate.py
# Description: Normalize the former schema-equivalent Alembic head, then upgrade to the canonical head.

import json
import uuid
from collections import Counter
from datetime import datetime, timezone

from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from app.utils import egress
from app.utils.postgres import AnthropicFallbackDb, PresetDb, ProxyEventDb
from app.utils.postgres.base import engine, init_database

CANONICAL_REVISION = "001"
# These revisions existed before the migration squash. Keeping their IDs here
# lets an existing installation normalize safely before Alembic loads the new
# single-file history. They are never recreated or replayed.
LEGACY_EQUIVALENT_HEADS = frozenset({"0011", "002", "003", "004", "005", "007", "008", "009", "010", "011", "012", "013"})
KNOWN_CHAIN_REVISIONS = frozenset({"001"})


def normalize_legacy_head() -> None:
    """Stamp every recognized historical head to the single canonical revision.

    The historical revision files are intentionally removed after the squash,
    so this normalization must run before Alembic resolves the current head.
    """
    init_database()
    with engine.begin() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return
        revisions = connection.execute(text("SELECT version_num FROM alembic_version FOR UPDATE")).scalars().all()
        recognized_revisions = KNOWN_CHAIN_REVISIONS | LEGACY_EQUIVALENT_HEADS
        if len(revisions) == 1 and revisions[0] in recognized_revisions:
            if revisions[0] != CANONICAL_REVISION:
                connection.execute(
                    text("UPDATE alembic_version SET version_num = :canonical WHERE version_num = :legacy"),
                    {"canonical": CANONICAL_REVISION, "legacy": revisions[0]},
                )
            return
        if len(revisions) != 1 or revisions[0] not in recognized_revisions:
            rendered = ", ".join(revisions) if revisions else "empty"
            raise RuntimeError(
                f"Unsupported Alembic state ({rendered}); expected one of "
                f"{sorted(KNOWN_CHAIN_REVISIONS)} or one of {sorted(LEGACY_EQUIVALENT_HEADS)}."
            )
        connection.execute(
            text("UPDATE alembic_version SET version_num = :canonical WHERE version_num = :legacy"),
            {"canonical": CANONICAL_REVISION, "legacy": revisions[0]},
        )


def sync_canonical_schema() -> None:
    """Apply additive fields introduced after the migration history was squashed."""
    with engine.begin() as connection:
        inspector = inspect(connection)
        if inspector.has_table("users"):
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            for column_name in ("rate_limit_per_hour", "rate_limit_per_day"):
                if column_name not in user_columns:
                    connection.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} INTEGER"))
        if not inspector.has_table("accounts"):
            return
        account_columns = {column["name"] for column in inspector.get_columns("accounts")}
        quota_columns = {
            "session_used_pct": "DOUBLE PRECISION",
            "session_reset_at": "TIMESTAMP WITH TIME ZONE",
            "weekly_used_pct": "DOUBLE PRECISION",
            "weekly_reset_at": "TIMESTAMP WITH TIME ZONE",
            "monthly_used_pct": "DOUBLE PRECISION",
            "monthly_reset_at": "TIMESTAMP WITH TIME ZONE",
        }
        for column_name, column_type in quota_columns.items():
            if column_name not in account_columns:
                connection.execute(text(f"ALTER TABLE accounts ADD COLUMN {column_name} {column_type}"))
        threshold_columns = {
            "five_hour_rotation_threshold": "DOUBLE PRECISION",
            "weekly_rotation_threshold": "DOUBLE PRECISION",
        }
        for column_name, column_type in threshold_columns.items():
            if column_name not in account_columns:
                connection.execute(text(f"ALTER TABLE accounts ADD COLUMN {column_name} {column_type} NOT NULL DEFAULT 1.0"))
                connection.execute(text(f"UPDATE accounts SET {column_name} = rotation_threshold WHERE rotation_threshold IS NOT NULL"))
        if "authenticated_override" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN authenticated_override BOOLEAN NOT NULL DEFAULT FALSE"))
        warmup_columns = {
            "warmup_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "warmup_next_at": "TIMESTAMP WITH TIME ZONE",
            "warmup_last_at": "TIMESTAMP WITH TIME ZONE",
            "warmup_last_status": "VARCHAR(32)",
            "warmup_last_error": "TEXT",
        }
        for column_name, column_type in warmup_columns.items():
            if column_name not in account_columns:
                connection.execute(text(f"ALTER TABLE accounts ADD COLUMN {column_name} {column_type}"))
        if "spare_capacity_tracking_json" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN spare_capacity_tracking_json TEXT NOT NULL DEFAULT '{}'"))

        # Automatic egress rotation is intentionally disabled. Persist the
        # first enabled configured target for every account so restored or
        # upgraded databases have the same deterministic assignment as new
        # accounts and request-time fallback.
        inspector = inspect(connection)
        account_columns = {column["name"] for column in inspector.get_columns("accounts")}
        if "egress_target_id" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN egress_target_id VARCHAR(128)"))
        default_egress_target = egress.get_pool().default_target().id
        connection.execute(
            text("UPDATE accounts SET egress_target_id = :target_id"),
            {"target_id": default_egress_target},
        )

        user_columns = {column["name"] for column in inspector.get_columns("users")}
        needs_preset_backfill = "preset_id" not in user_columns or not inspector.has_table("presets")
        user_additions = {
            "priority": "INTEGER NOT NULL DEFAULT 1",
            "fallback_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
            "spare_capacity_only": "BOOLEAN NOT NULL DEFAULT FALSE",
            "lifetime_token_budget": "BIGINT",
            "monthly_spend_budget_usd": "DOUBLE PRECISION",
            "lifetime_spend_budget_usd": "DOUBLE PRECISION",
            "model_overrides_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column_name, column_type in user_additions.items():
            if column_name not in user_columns:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}"))
        if "allow_extended_context" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN allow_extended_context BOOLEAN NOT NULL DEFAULT FALSE"))
        preset_columns = {
            "allowed_models_json": "TEXT",
            "preset_id": "UUID",
            "preset_overrides_json": "TEXT NOT NULL DEFAULT '[]'",
            "model_thinking_levels_json": "TEXT NOT NULL DEFAULT '{}'",
            "allowed_thinking_modes_json": 'TEXT NOT NULL DEFAULT \'["disabled","enabled","adaptive"]\'',
            "model_thinking_modes_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column_name, column_type in preset_columns.items():
            if column_name not in user_columns:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}"))
        PresetDb.__table__.create(connection, checkfirst=True)
        preset_columns_existing = {column["name"] for column in inspect(connection).get_columns("presets")}
        if "allow_extended_context" not in preset_columns_existing:
            connection.execute(text("ALTER TABLE presets ADD COLUMN allow_extended_context BOOLEAN NOT NULL DEFAULT FALSE"))
        preset_fields = (
            "allowed_models_json",
            "allowed_thinking_levels",
            "model_overrides_json",
            "model_thinking_levels_json",
            "allowed_thinking_modes_json",
            "model_thinking_modes_json",
            "allow_extended_context",
        )
        existing_preset = connection.execute(text("SELECT id FROM presets ORDER BY created_at LIMIT 1")).scalar()
        if existing_preset is None:
            existing_users = connection.execute(text(f"SELECT id, {', '.join(preset_fields)} FROM users")).mappings().all()
            defaults = {
                "allowed_models_json": None,
                "allowed_thinking_levels": ["low", "medium", "high", "max"],
                "model_overrides_json": "{}",
                "model_thinking_levels_json": "{}",
                "allowed_thinking_modes_json": '["disabled","enabled","adaptive"]',
                "model_thinking_modes_json": "{}",
                "allow_extended_context": False,
            }
            baseline = {}
            for field, default in defaults.items():
                if not existing_users:
                    baseline[field] = default
                elif field == "allowed_thinking_levels":
                    common = Counter(tuple(row[field]) for row in existing_users).most_common(1)[0][0]
                    baseline[field] = list(common)
                else:
                    baseline[field] = Counter(row[field] for row in existing_users).most_common(1)[0][0]
            existing_preset = uuid.uuid4()
            connection.execute(
                text(
                    f"INSERT INTO presets (id, name, {', '.join(preset_fields)}, created_at) "
                    f"VALUES (:id, :name, {', '.join(':' + field for field in preset_fields)}, :created_at)"
                ),
                {"id": existing_preset, "name": "Current configuration", **baseline, "created_at": datetime.now(timezone.utc)},
            )
        else:
            baseline = (
                connection.execute(text(f"SELECT {', '.join(preset_fields)} FROM presets WHERE id = :id"), {"id": existing_preset}).mappings().one()
            )
        # Only backfill a legacy schema once. A NULL preset_id on an already-migrated
        # schema is an intentional direct user policy and must survive restarts.
        if needs_preset_backfill:
            for row in connection.execute(text(f"SELECT id, {', '.join(preset_fields)} FROM users WHERE preset_id IS NULL")).mappings():
                overrides = [field.removesuffix("_json") for field in preset_fields if row[field] != baseline[field]]
                connection.execute(
                    text("UPDATE users SET preset_id = :preset_id, preset_overrides_json = :overrides WHERE id = :id"),
                    {"preset_id": existing_preset, "overrides": json.dumps(overrides), "id": row["id"]},
                )
        connection.execute(text("UPDATE users SET allow_extended_context = TRUE WHERE lower(trim(name)) = 'devasheesh'"))

        # Repair the protected user's existing override marker without
        # changing other policy choices. Repeat migrations are idempotent.
        for row in (
            connection.execute(text("SELECT id, preset_overrides_json FROM users WHERE lower(trim(name)) = 'devasheesh' AND preset_id IS NOT NULL"))
            .mappings()
            .all()
        ):
            overrides = set(json.loads(row["preset_overrides_json"] or "[]"))
            overrides.add("allow_extended_context")
            connection.execute(
                text("UPDATE users SET preset_overrides_json = :overrides WHERE id = :id"),
                {"overrides": json.dumps(sorted(overrides)), "id": row["id"]},
            )

        # Reconcile fallback routing additions without inventing a second
        # migration history.
        AnthropicFallbackDb.__table__.create(connection, checkfirst=True)
        inspector = inspect(connection)
        fallback_columns = {column["name"] for column in inspector.get_columns("anthropic_fallbacks")}
        if "egress_target_id" not in fallback_columns:
            connection.execute(text("ALTER TABLE anthropic_fallbacks ADD COLUMN egress_target_id VARCHAR(128)"))
        default_egress_target = egress.get_pool().default_target().id
        connection.execute(
            text("UPDATE anthropic_fallbacks SET egress_target_id = :target_id"),
            {"target_id": default_egress_target},
        )
        inspector = inspect(connection)
        usage_columns = {column["name"] for column in inspector.get_columns("usage_records")}
        if "fallback_provider_id" not in usage_columns:
            connection.execute(
                text("ALTER TABLE usage_records ADD COLUMN fallback_provider_id UUID REFERENCES anthropic_fallbacks(id) ON DELETE SET NULL")
            )
        if "billed_cost_usd" not in usage_columns:
            connection.execute(text("ALTER TABLE usage_records ADD COLUMN billed_cost_usd DOUBLE PRECISION"))
        if "duration_ms" not in usage_columns:
            connection.execute(text("ALTER TABLE usage_records ADD COLUMN duration_ms DOUBLE PRECISION"))
        if "tokens_per_second" not in usage_columns:
            connection.execute(text("ALTER TABLE usage_records ADD COLUMN tokens_per_second DOUBLE PRECISION"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_records_fallback_provider_id ON usage_records (fallback_provider_id)"))

        events_existed = inspect(connection).has_table("proxy_events")
        ProxyEventDb.__table__.create(connection, checkfirst=True)
        if not events_existed:
            connection.execute(
                text(
                    """
                    INSERT INTO proxy_events
                        (id, created_at, request_id, user_id, api_key_id, account_id,
                         fallback_provider_id, event_type, status_code, message, metadata_json)
                    SELECT gen_random_uuid(), u.created_at,
                           COALESCE(NULLIF(u.request_id, ''), 'req_legacy_' || replace(u.id::text, '-', '')),
                           u.user_id, u.account_id, u.api_key_id, u.fallback_provider_id,
                           'response.returned', u.status_code, 'Migrated from request history',
                           json_build_object('model', u.model, 'reasoning_level', u.reasoning_level,
                             'input_tokens', u.input_tokens, 'output_tokens', u.output_tokens,
                             'cache_read_input_tokens', u.cache_read_input_tokens,
                             'cache_write_tokens', u.cache_creation_input_tokens,
                             'cost_usd', u.billed_cost_usd)::text
                    FROM usage_records u
                    WHERE NOT EXISTS (
                        SELECT 1 FROM proxy_events e
                        WHERE e.event_type = 'response.returned'
                          AND e.request_id = COALESCE(NULLIF(u.request_id, ''), 'req_legacy_' || replace(u.id::text, '-', ''))
                    )
                    """
                )
            )

        # The old candidate-selection event names were replaced by names that
        # describe the response timing. Reconcile historical rows here so the
        # data migration remains part of the canonical single-revision flow.
        connection.execute(text("UPDATE proxy_events SET event_type = 'account.response_received' WHERE event_type = 'account.selected'"))
        connection.execute(text("UPDATE proxy_events SET event_type = 'fallback.response_received' WHERE event_type = 'fallback.selected'"))
        connection.execute(
            text("""
            WITH timings AS (
                SELECT request_id,
                       min(created_at) AS started_at
                FROM proxy_events
                WHERE request_id IS NOT NULL AND event_type = 'request.received'
                GROUP BY request_id
            )
            UPDATE usage_records u
            SET duration_ms = EXTRACT(EPOCH FROM (u.created_at - t.started_at)) * 1000.0,
                tokens_per_second = CASE
                    WHEN u.output_tokens > 0 AND u.created_at > t.started_at
                    THEN u.output_tokens / EXTRACT(EPOCH FROM (u.created_at - t.started_at))
                    ELSE NULL
                END
            FROM timings t
            WHERE u.request_id = t.request_id
              AND u.duration_ms IS NULL
              AND t.started_at IS NOT NULL
              AND u.created_at > t.started_at
        """)
        )


def main() -> None:
    normalize_legacy_head()
    command.upgrade(Config("alembic.ini"), "head")
    sync_canonical_schema()


if __name__ == "__main__":
    main()
