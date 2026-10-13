# Path: app/utils/postgres/base.py
# Description: Database client for PostgreSQL.

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy_utils import create_database, database_exists

from app.config import get_settings

# Get the settings
settings = get_settings()

# Create the engine
engine = create_engine(
    settings.get_postgres_url(),
    pool_size=settings.DB_POOL_SIZE,  # connections kept open per process
    max_overflow=settings.DB_MAX_OVERFLOW,  # extra connections opened under load, closed when returned
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,  # wait this long for a free connection before failing
    pool_pre_ping=True,  # drop stale connections before use
    pool_recycle=1800,  # replace connections before intermediaries (NAT, PgBouncer) silently drop them
    connect_args={"options": "-c timezone=utc", "application_name": "claude-code-proxy"},
)

# Create the session factory
SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create the base class
DatabaseBase = declarative_base()


def init_database() -> None:
    """Create the target database if it does not yet exist. Called by the Alembic entrypoint before migrating."""
    url = settings.get_postgres_url()
    if not database_exists(url):
        create_database(url)


def get_db():
    """Get Database Session."""
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_cm() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    db = SessionFactory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
