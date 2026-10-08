"""Ingest tests: line parsing (pure) and storing samples (database)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.ingest import BIGINT_MAX, MetricMessage, parse_line, store_message
from app.main import record_silent_hosts
from app.models import Anomaly, Host, Metric

MakeMsg = Callable[..., MetricMessage]

VALID = {
    "host": "agent-1",
    "ts": 1_760_000_000,
    "cpu_percent": 37.5,
    "mem_used_mb": 2048,
    "mem_total_mb": 8192,
    "net_rx_bytes": 123456,
    "net_tx_bytes": 65432,
}


def line(**overrides: object) -> bytes:
    """A JSON line built from VALID; a value of None removes that field."""
    fields = {**VALID, **overrides}
    fields = {k: v for k, v in fields.items() if v is not None}
    return json.dumps(fields).encode() + b"\n"


# --- parse_line -------------------------------------------------------------


def test_parse_valid_line() -> None:
    msg = parse_line(line())
    assert msg is not None
    assert msg.host == "agent-1"
    assert msg.cpu_percent == 37.5
    assert msg.net_rx_bytes == 123456


def test_parse_ignores_unknown_fields() -> None:
    assert parse_line(line(agent_version="2.0")) is not None


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(b"not json\n", id="not-json"),
        pytest.param(b"\n", id="empty"),
        pytest.param(b"\xff\xfe\n", id="invalid-utf8"),
        pytest.param(b"[1, 2]\n", id="not-an-object"),
        pytest.param(line(cpu_percent=None), id="missing-field"),
        pytest.param(line(host=""), id="empty-host"),
        pytest.param(line(ts="1760000000"), id="ts-as-string"),
        pytest.param(line(ts=True), id="ts-as-bool"),
        pytest.param(line(ts=-5), id="negative-ts"),
        pytest.param(line(cpu_percent=150), id="cpu-above-100"),
        pytest.param(line(cpu_percent=-1), id="cpu-negative"),
        pytest.param(line(mem_used_mb=2048.5), id="mem-not-integer"),
        pytest.param(line(net_rx_bytes=BIGINT_MAX + 1), id="counter-over-bigint"),
    ],
)
def test_parse_rejects_bad_line(bad: bytes) -> None:
    assert parse_line(bad) is None


# --- store_message ------------------------------------------------------------


def test_store_creates_host_and_metric(session: Session, make_msg: MakeMsg) -> None:
    store_message(make_msg(cpu_percent=42.0))

    host = session.exec(select(Host)).one()
    metric = session.exec(select(Metric)).one()
    assert host.name == "agent-1"
    assert metric.host_id == host.id
    assert metric.cpu_percent == 42.0
    assert metric.ts == datetime.fromtimestamp(1_760_000_000, UTC)


def test_store_same_host_twice_updates_it(session: Session, make_msg: MakeMsg) -> None:
    store_message(make_msg(ts=1_760_000_000))
    store_message(make_msg(ts=1_760_000_002))

    host = session.exec(select(Host)).one()  # .one(): exactly one row, no duplicate
    assert host.last_seen > host.first_seen
    assert len(session.exec(select(Metric)).all()) == 2


def anomaly_types(session: Session) -> list[str]:
    return [a.type for a in session.exec(select(Anomaly).order_by(Anomaly.id)).all()]


def test_long_cpu_spike_creates_one_anomaly(session: Session, make_msg: MakeMsg) -> None:
    for i in range(6):
        store_message(make_msg(ts=1_760_000_000 + 2 * i, cpu_percent=99.0))

    assert anomaly_types(session) == ["cpu_high"]


def test_two_cpu_spikes_create_two_anomalies(session: Session, make_msg: MakeMsg) -> None:
    # Three quiet samples between the spikes end the first event.
    for i, cpu in enumerate([99, 99, 99, 5, 5, 5, 99, 99, 99]):
        store_message(make_msg(ts=1_760_000_000 + 2 * i, cpu_percent=float(cpu)))

    assert anomaly_types(session) == ["cpu_high", "cpu_high"]


def test_flapping_cpu_creates_one_anomaly(session: Session, make_msg: MakeMsg) -> None:
    # The pattern from the MCP demo: an unsteady load with short dips. Before hysteresis
    # every dip below 90% ended the event, and one load gave 4 anomalies.
    pattern = [99, 99, 99, 59.9, 94, 95, 97, 88, 91, 93, 96, 85, 99, 99, 99]
    for i, cpu in enumerate(pattern):
        store_message(make_msg(ts=1_760_000_000 + 2 * i, cpu_percent=float(cpu)))

    assert anomaly_types(session) == ["cpu_high"]


def test_cpu_events_are_tracked_per_host(session: Session, make_msg: MakeMsg) -> None:
    # An open event on one host must not silence the rule on another.
    for i in range(3):
        store_message(make_msg(host="agent-1", ts=1_760_000_000 + 2 * i, cpu_percent=99.0))
    for i in range(3):
        store_message(make_msg(host="agent-2", ts=1_760_000_010 + 2 * i, cpu_percent=99.0))

    assert anomaly_types(session) == ["cpu_high", "cpu_high"]


def test_same_timestamp_keeps_arrival_order(session: Session, make_msg: MakeMsg) -> None:
    # All samples share one ts, so only the id tie-breaker gives the order.
    for used in [500, 950, 960, 500, 950]:
        store_message(make_msg(mem_used_mb=used, mem_total_mb=1000))

    assert anomaly_types(session) == ["memory_high", "memory_high"]


# --- record_silent_hosts ------------------------------------------------------


def test_silence_reported_once_per_silence(session: Session) -> None:
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    host = Host(name="agent-1", first_seen=t0, last_seen=t0)
    session.add(host)
    session.commit()

    record_silent_hosts(now=t0 + timedelta(seconds=20))  # not silent yet
    assert anomaly_types(session) == []

    record_silent_hosts(now=t0 + timedelta(seconds=40))
    record_silent_hosts(now=t0 + timedelta(seconds=50))  # same silence
    assert anomaly_types(session) == ["host_silent"]

    # The host comes back, then goes silent again: a new silence.
    host.last_seen = t0 + timedelta(seconds=60)
    session.commit()
    record_silent_hosts(now=t0 + timedelta(seconds=70))
    assert anomaly_types(session) == ["host_silent"]
    record_silent_hosts(now=t0 + timedelta(seconds=100))
    assert anomaly_types(session) == ["host_silent", "host_silent"]


def test_lasting_cpu_change_creates_one_unusual_anomaly(
    session: Session, make_msg: MakeMsg
) -> None:
    # A host at ~5% for 40 samples, then at 60% for 20 samples: one cpu_unusual (not one per
    # sample), and no cpu_high, because 60% is below the fixed threshold.
    pattern = [4.0, 5.0, 6.0, 5.0] * 10 + [60.0] * 20
    for i, cpu in enumerate(pattern):
        store_message(make_msg(ts=1_760_000_000 + 2 * i, cpu_percent=cpu))

    assert anomaly_types(session) == ["cpu_unusual"]

