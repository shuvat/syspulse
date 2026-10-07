"""Database engine and session helpers."""

import os
from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (registers the tables on SQLModel.metadata)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://syspulse:syspulse@localhost:5432/syspulse"
)

# pool_pre_ping checks a pooled connection before use, so a database restart
# does not make the next request fail on a dead connection.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def create_db_and_tables() -> None:
    """Create missing tables. Existing tables are not altered."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """Yield a session that is closed afterwards (used as a FastAPI dependency)."""
    with Session(engine) as session:
        yield session
