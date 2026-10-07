"""REST API tests with FastAPI's TestClient."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine

from app.db import get_session
from app.ingest import MetricMessage, store_message
from app.main import app
from app.models import Anomaly, Host

MakeMsg = Callable[..., MetricMessage]

# Without `with TestClient(app)` the lifespan does not run, so no TCP server
# or background task is started; the database is set up by the fixtures.
client = TestClient(app)


def now_ts(minutes_ago: int = 0) -> int:
    return int((datetime.now(UTC) - timedelta(minutes=minutes_ago)).timestamp())


def test_health_ok(session: Session) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_database_down() -> None:
    # Swap the session dependency for one bound to a port nothing listens on.
    dead_engine = create_engine("postgresql+psycopg://x:x@127.0.0.1:1/x")

    def dead_session():
        with Session(dead_engine) as session:
            yield session

    app.dependency_overrides[get_session] = dead_session
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503


def test_hosts_sorted_by_name(session: Session, make_msg: MakeMsg) -> None:
    store_message(make_msg(host="beta"))
    store_message(make_msg(host="alpha"))

    response = client.get("/hosts")
    assert response.status_code == 200
    assert [h["name"] for h in response.json()] == ["alpha", "beta"]
    assert set(response.json()[0]) == {"name", "first_seen", "last_seen"}  # no internal id


def test_host_metrics_oldest_first(session: Session, make_msg: MakeMsg) -> None:
    store_message(make_msg(ts=now_ts(2), cpu_percent=20.0))
    store_message(make_msg(ts=now_ts(1), cpu_percent=10.0))

    response = client.get("/hosts/agent-1/metrics")
    assert response.status_code == 200
    assert [m["cpu_percent"] for m in response.json()] == [20.0, 10.0]
    assert "host_id" not in response.json()[0]


def test_host_metrics_time_window(session: Session, make_msg: MakeMsg) -> None:
    store_message(make_msg(ts=now_ts(20)))
    store_message(make_msg(ts=now_ts(1)))

    assert len(client.get("/hosts/agent-1/metrics?minutes=10").json()) == 1
    assert len(client.get("/hosts/agent-1/metrics?minutes=30").json()) == 2


def test_host_metrics_unknown_host(session: Session) -> None:
    response = client.get("/hosts/nope/metrics")
    assert response.status_code == 404


@pytest.mark.parametrize("minutes", ["0", "1441", "abc"])
def test_host_metrics_invalid_minutes(session: Session, minutes: str) -> None:
    response = client.get(f"/hosts/agent-1/metrics?minutes={minutes}")
    assert response.status_code == 422


def test_anomalies_include_host_name(session: Session, make_msg: MakeMsg) -> None:
    for i in range(3):
        store_message(make_msg(ts=now_ts() - 6 + 2 * i, cpu_percent=99.0))

    response = client.get("/anomalies")
    assert response.status_code == 200
    [anomaly] = response.json()
    assert anomaly["host"] == "agent-1"
    assert anomaly["type"] == "cpu_high"


def test_anomalies_time_window(session: Session) -> None:
    now = datetime.now(UTC)
    host = Host(name="agent-1", first_seen=now, last_seen=now)
    session.add(host)
    session.flush()  # assigns host.id
    for minutes_ago in [90, 5]:
        session.add(
            Anomaly(
                host_id=host.id,
                ts=now - timedelta(minutes=minutes_ago),
                type="cpu_high",
                value=99.0,
                message="test",
            )
        )
    session.commit()

    assert len(client.get("/anomalies").json()) == 1  # default: last 60 minutes
    assert len(client.get("/anomalies?minutes=120").json()) == 2
