"""Create the complete Claude Code Proxy schema.

Revision ID: 001
Revises:

This is the canonical initial migration for fresh installations. Databases
created from the former migration history are stamped to this equivalent head
without replaying schema operations.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.utils.models.api import AccountStatus, ProviderHealth

revision: str = "001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

account_status_enum = postgresql.ENUM(
    *[status.value for status in AccountStatus],
    name="account_status",
    create_type=False,
)
provider_health_enum = postgresql.ENUM(
    *[health.value for health in ProviderHealth],
    name="provider_health",
    create_type=False,
)


def upgrade() -> None:
    account_status_enum.create(op.get_bind(), checkfirst=True)
    provider_health_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("label", sa.VARCHAR(), nullable=False),
        sa.Column("account_email", sa.VARCHAR(), nullable=True),
        # Stable outbound route selected for this account; null uses the
        # configured default target.
        sa.Column("egress_target_id", sa.VARCHAR(length=128), nullable=True),
        sa.Column("authenticated_override", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("tier", sa.VARCHAR(), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", account_status_enum, nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_health", provider_health_enum, nullable=False, server_default="UNKNOWN"),
        sa.Column("provider_health_code", sa.VARCHAR(), nullable=True),
        sa.Column("provider_health_message", sa.VARCHAR(), nullable=True),
        sa.Column("provider_health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_health_last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_health_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("session_used_pct", sa.Float(), nullable=True),
        sa.Column("session_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("weekly_used_pct", sa.Float(), nullable=True),
        sa.Column("weekly_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("monthly_used_pct", sa.Float(), nullable=True),
        sa.Column("monthly_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("five_hour_rotation_threshold", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("weekly_rotation_threshold", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("rotation_threshold", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("max_failover_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warmup_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("warmup_next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warmup_last_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warmup_last_status", sa.VARCHAR(length=32), nullable=True),
        sa.Column("warmup_last_error", sa.Text(), nullable=True),
        sa.Column("spare_capacity_tracking_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("quota_attribution_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_accounts_id"),
    )
    op.create_index("ix_accounts_status", "accounts", ["status"])
    op.create_index("ix_accounts_priority", "accounts", ["priority"])

    op.create_table(
        "presets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.VARCHAR(length=120), nullable=False),
        sa.Column("fallback_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("allowed_models_json", sa.Text(), nullable=True),
        sa.Column("allowed_thinking_levels", postgresql.ARRAY(sa.VARCHAR()), nullable=False, server_default="{low,medium,high,max}"),
        sa.Column("model_overrides_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("model_thinking_levels_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("allowed_thinking_modes_json", sa.Text(), nullable=False, server_default='["disabled","enabled","adaptive"]'),
        sa.Column("model_thinking_modes_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("allow_extended_context", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_presets_id"),
        sa.UniqueConstraint("name", name="uq_presets_name"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.VARCHAR(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        # Fallback is opt-in; migration 004 changed the historical default to
        # false, so fresh databases must start with the same safe behavior.
        sa.Column("fallback_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("spare_capacity_only", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("rate_limit_per_hour", sa.Integer(), nullable=True),
        sa.Column("rate_limit_per_day", sa.Integer(), nullable=True),
        sa.Column("monthly_token_budget", sa.BigInteger(), nullable=True),
        sa.Column("lifetime_token_budget", sa.BigInteger(), nullable=True),
        sa.Column("monthly_spend_budget_usd", sa.Float(), nullable=True),
        sa.Column("lifetime_spend_budget_usd", sa.Float(), nullable=True),
        sa.Column(
            "allowed_thinking_levels",
            postgresql.ARRAY(sa.VARCHAR()),
            nullable=False,
            server_default="{low,medium,high,max}",
        ),
        sa.Column("model_overrides_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("allowed_models_json", sa.Text(), nullable=True),
        sa.Column("preset_id", sa.UUID(), nullable=True),
        sa.Column("preset_overrides_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("model_thinking_levels_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("allowed_thinking_modes_json", sa.Text(), nullable=False, server_default='["disabled","enabled","adaptive"]'),
        sa.Column("model_thinking_modes_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("allow_extended_context", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users_id"),
    )
    op.create_index("ix_users_name", "users", ["name"])

    op.create_table(
        "dashboard_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("username", sa.VARCHAR(length=120), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("permissions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_dashboard_members_id"),
        sa.UniqueConstraint("username", name="uq_dashboard_members_username"),
    )
    op.create_index("ix_dashboard_members_active", "dashboard_members", ["active"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.VARCHAR(), nullable=True),
        sa.Column("key_prefix", sa.VARCHAR(), nullable=False),
        sa.Column("key_hash", sa.VARCHAR(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("monthly_token_budget", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_api_keys_user_id", ondelete="CASCADE"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])

    op.create_table(
        "anthropic_fallbacks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("label", sa.VARCHAR(length=200), nullable=False),
        sa.Column("base_url", sa.VARCHAR(length=2048), nullable=False),
        sa.Column("api_key_enc", sa.Text(), nullable=False),
        sa.Column("credential_hash", sa.VARCHAR(length=64), nullable=False),
        sa.Column("key_hint", sa.VARCHAR(length=32), nullable=False),
        sa.Column("status", account_status_enum, nullable=False, server_default="ACTIVE"),
        sa.Column("provider_health", provider_health_enum, nullable=False, server_default="UNKNOWN"),
        sa.Column("provider_health_message", sa.VARCHAR(), nullable=True),
        sa.Column("provider_health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("monthly_spend_limit_usd", sa.Float(), nullable=True),
        sa.Column("model_catalog_json", sa.Text(), nullable=True),
        sa.Column("model_catalog_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("egress_target_id", sa.VARCHAR(length=128), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_anthropic_fallbacks_id"),
        sa.UniqueConstraint("credential_hash", name="uq_anthropic_fallbacks_credential_hash"),
    )
    op.create_index(
        "ix_anthropic_fallbacks_status_priority",
        "anthropic_fallbacks",
        ["status", "priority"],
    )

    op.create_table(
        "usage_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("api_key_id", sa.UUID(), nullable=True),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("fallback_provider_id", sa.UUID(), nullable=True),
        sa.Column("model", sa.VARCHAR(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_creation_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_creation_5m_input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_creation_1h_input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reasoning_level", sa.VARCHAR(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.VARCHAR(), nullable=True),
        sa.Column("billed_cost_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_usage_records_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_usage_records_user_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], name="fk_usage_records_api_key_id", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name="fk_usage_records_account_id", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["fallback_provider_id"],
            ["anthropic_fallbacks.id"],
            name="fk_usage_records_fallback_provider_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_usage_records_user_id", "usage_records", ["user_id"])
    op.create_index("ix_usage_records_api_key_id", "usage_records", ["api_key_id"])
    op.create_index("ix_usage_records_account_id", "usage_records", ["account_id"])
    op.create_index("ix_usage_records_fallback_provider_id", "usage_records", ["fallback_provider_id"])
    op.create_index("ix_usage_records_created_at", "usage_records", ["created_at"])

    op.create_table(
        "proxy_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("api_key_id", sa.UUID(), nullable=True),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("fallback_provider_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_proxy_events_id"),
    )
    op.create_index("ix_proxy_events_created_at", "proxy_events", ["created_at"])
    op.create_index("ix_proxy_events_request_id", "proxy_events", ["request_id"])
    op.create_index("ix_proxy_events_user_id", "proxy_events", ["user_id"])

    _create_notification_tables()


def _create_notification_tables() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("channel_type", sa.VARCHAR(), nullable=False),
        sa.Column("name", sa.VARCHAR(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("secret_enc", sa.Text(), nullable=True),
        sa.Column("destination", sa.VARCHAR(), nullable=True),
        sa.Column("thread_id", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.VARCHAR(), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notification_channels_id"),
        sa.UniqueConstraint("channel_type", name="uq_notification_channels_type"),
    )
    op.create_index("ix_notification_channels_enabled", "notification_channels", ["enabled"])

    op.create_table(
        "notification_rules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.VARCHAR(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notification_rules_id"),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            name="fk_notification_rules_channel_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("channel_id", "event_type", name="uq_notification_rules_channel_event"),
    )
    op.create_index("ix_notification_rules_event_type", "notification_rules", ["event_type"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.VARCHAR(), nullable=False),
        sa.Column("event_key", sa.VARCHAR(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.VARCHAR(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification_deliveries_id"),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            name="fk_notification_deliveries_channel_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("channel_id", "event_type", "event_key", name="uq_notification_deliveries_event"),
    )
    op.create_index("ix_notification_deliveries_dispatch", "notification_deliveries", ["status", "available_at"])


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("notification_rules")
    op.drop_table("notification_channels")
    op.drop_table("proxy_events")
    op.drop_table("usage_records")
    op.drop_table("anthropic_fallbacks")
    op.drop_table("dashboard_members")
    op.drop_table("api_keys")
    op.drop_table("users")
    op.drop_table("presets")
    op.drop_table("accounts")
    provider_health_enum.drop(op.get_bind(), checkfirst=True)
    account_status_enum.drop(op.get_bind(), checkfirst=True)
