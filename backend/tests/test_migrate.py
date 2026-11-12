"""Guarded live-schema reconciliation tests for the squashed migration."""

from sqlalchemy import inspect, text

from app.scripts import migrate
from app.utils.postgres.base import engine


def test_sync_canonical_schema_adds_rotation_threshold_and_fallback_columns():
    threshold_columns = {"five_hour_rotation_threshold", "weekly_rotation_threshold"}
    additive_columns = threshold_columns | {"authenticated_override", "has_five_hour_limit", "scoped_limits_json"}
    user_additions = {
        "lifetime_token_budget",
        "monthly_spend_budget_usd",
        "lifetime_spend_budget_usd",
        "model_overrides_json",
        "five_hour_limit_budget",
        "weekly_limit_budget",
    }
    with engine.begin() as connection:
        for column_name in threshold_columns:
            connection.execute(text(f"ALTER TABLE accounts DROP COLUMN {column_name}"))
        connection.execute(text("ALTER TABLE accounts DROP COLUMN authenticated_override"))
        connection.execute(text("ALTER TABLE accounts DROP COLUMN has_five_hour_limit"))
        connection.execute(text("ALTER TABLE accounts DROP COLUMN scoped_limits_json"))
        connection.execute(text("ALTER TABLE api_keys DROP COLUMN weekly_limit_budget"))
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
    assert "weekly_limit_budget" in {column["name"] for column in inspect(engine).get_columns("api_keys")}


def test_concurrent_indexes_are_idempotent_and_replace_invalid_builds():
    from app.scripts.migrate import CONCURRENT_INDEXES, ensure_concurrent_indexes

    with engine.begin() as connection:
        for name, _, _ in CONCURRENT_INDEXES:
            connection.execute(text(f"DROP INDEX IF EXISTS {name}"))
        # Simulate an interrupted CREATE INDEX CONCURRENTLY, which leaves an invalid index behind.
        connection.execute(text("CREATE INDEX ix_usage_records_user_created ON usage_records (user_id, created_at)"))
        connection.execute(text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'ix_usage_records_user_created'::regclass"))
    ensure_concurrent_indexes()
    ensure_concurrent_indexes()
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:names)"),
            {"names": [name for name, _, _ in CONCURRENT_INDEXES]},
        ).all()
    assert sorted(rows) == sorted((name, True) for name, _, _ in CONCURRENT_INDEXES)
