# Path: alembic/env.py
# Description: This file contains the code to run migrations for the database.

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import get_settings

settings = get_settings()

config = context.config
# ConfigParser treats "%" as interpolation, so escape it in case the password contains one.
config.set_main_option("sqlalchemy.url", settings.get_postgres_uri().replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import app.utils.postgres.schemas  # noqa: E402, F401
from app.utils.postgres.base import DatabaseBase, init_database  # noqa: E402

target_metadata = DatabaseBase.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The database is no longer created at import time, so ensure it exists before we connect.
    init_database()

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
