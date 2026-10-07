"""Shared fixtures. Tests that touch the database use a separate PostgreSQL
database (syspulse_test), so development data is never affected."""

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://syspulse:syspulse@localhost:5432/syspulse_test",
)
# Must happen before anything imports `app`: app.db builds the engine at import time.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from collections.abc import Callable, Iterator  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg import sql  # noqa: E402
from sqlalchemy import make_url, text  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from app.db import engine  # noqa: E402
from app.ingest import MetricMessage  # noqa: E402


def _create_test_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    # Guard against truncating a real database by mistake.
    assert url.database and url.database.endswith("_test"), "tests need a *_test database"
    # CREATE DATABASE cannot run inside a transaction, hence autocommit.
    with psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))


@pytest.fixture(scope="session")
def database() -> Iterator[None]:
    """Once per test run: create the database and a fresh schema."""
    _create_test_database()
    # Drop first, so the tables always match the current models.
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture
def session(database: None) -> Iterator[Session]:
    """Per test: empty tables and a session for setup and assertions."""
    with Session(engine) as session:
        session.execute(text("TRUNCATE metrics, anomalies, hosts RESTART IDENTITY"))
        session.commit()
        yield session


@pytest.fixture
def make_msg() -> Callable[..., MetricMessage]:
    """Factory for valid messages; tests override only the fields they care about."""

    def make(**overrides: object) -> MetricMessage:
        fields: dict[str, object] = {
            "host": "agent-1",
            "ts": 1_760_000_000,
            "cpu_percent": 5.0,
            "mem_used_mb": 100,
            "mem_total_mb": 1000,
            "net_rx_bytes": 0,
            "net_tx_bytes": 0,
        }
        fields.update(overrides)
        return MetricMessage(**fields)

    return make
