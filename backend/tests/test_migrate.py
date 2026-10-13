"""Guarded live-schema reconciliation tests for the squashed migration."""

from sqlalchemy import inspect, text

from app.scripts import migrate
from app.utils.postgres.base import engine


def test_sync_canonical_schema_adds_rotation_threshold_and_fallback_columns():
    threshold_columns = {"five_hour_rotation_threshold", "weekly_rotation_threshold"}
    additive_columns = threshold_columns | {"authenticated_override"}
    user_additions = {
        "lifetime_token_budget",
        "monthly_spend_budget_usd",
        "lifetime_spend_budget_usd",
        "model_overrides_json",
    }
    with engine.begin() as connection:
        for column_name in threshold_columns:
            connection.execute(text(f"ALTER TABLE accounts DROP COLUMN {column_name}"))
        connection.execute(text("ALTER TABLE accounts DROP COLUMN authenticated_override"))
        connection.execute(text("ALTER TABLE usage_records DROP COLUMN fallback_provider_id"))
        connection.execute(text("ALTER TABLE usage_records DROP COLUMN billed_cost_usd"))
        for column_name in user_additions:
            connection.execute(text(f"ALTER TABLE users DROP COLUMN {column_name}"))
        connection.execute(text("DROP TABLE anthropic_fallbacks"))
    assert threshold_columns.isdisjoint({column["name"] for column in inspect(engine).get_columns("accounts")})

    migrate.sync_canonical_schema()

    account_columns = {column["name"] for column in inspect(engine).get_columns("accounts")}
    assert additive_columns <= account_columns
    usage_columns = {column["name"] for column in inspect(engine).get_columns("usage_records")}
    assert inspect(engine).has_table("anthropic_fallbacks")
    assert {"fallback_provider_id", "billed_cost_usd"} <= usage_columns
    assert user_additions <= {column["name"] for column in inspect(engine).get_columns("users")}
