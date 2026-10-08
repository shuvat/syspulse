"""Pure rule tests: no database, no real clock."""

from datetime import UTC, datetime, timedelta

import pytest

from app.anomalies import (
    check_cpu,
    check_memory,
    check_silence,
    cpu_event_open,
    memory_percent,
    watcher_was_paused,
)

HIGH = 95.0
LOW = 10.0


@pytest.mark.parametrize(
    ("cpu_since_alert", "is_open"),
    [
        (None, False),  # the host never had a cpu_high anomaly
        ([], True),  # alert just fired, no sample since
        ([96.0, 59.9, 94.0, 95.0], True),  # the demo's single dip does not end it
        ([85.0, 85.0, 85.0, 85.0], True),  # below 90 but not below 80: hysteresis band
        ([50.0, 50.0, HIGH], True),  # only 2 samples below 80
        ([50.0, 50.0, 50.0], False),  # 3 consecutive samples below 80: ended
        ([50.0, 50.0, 50.0, HIGH, HIGH], False),  # ended, then high again: a new event
        ([79.9, 79.9, 79.9], False),
        ([80.0, 80.0, 80.0], True),  # exactly at the clear threshold is not below
    ],
)
def test_cpu_event_open(cpu_since_alert: list[float] | None, is_open: bool) -> None:
    assert cpu_event_open(cpu_since_alert) == is_open


@pytest.mark.parametrize(
    ("samples", "fires"),
    [
        ([], False),
        ([HIGH, HIGH], False),  # streak too short
        ([HIGH, HIGH, HIGH], True),  # new host whose first samples are high
        ([LOW, HIGH, HIGH, HIGH], True),  # streak just reached 3
        ([LOW, HIGH, LOW, HIGH], False),  # not consecutive
        ([HIGH, HIGH, HIGH, LOW], False),  # streak ended
        ([LOW, 90.0, 90.0, 90.0], False),  # exactly at the threshold is not above
        ([LOW, 90.1, 90.1, 90.1], True),
    ],
)
def test_check_cpu_without_open_event(samples: list[float], fires: bool) -> None:
    assert (check_cpu(samples, event_open=False) is not None) == fires


def test_check_cpu_quiet_while_event_open() -> None:
    assert check_cpu([LOW, HIGH, HIGH, HIGH], event_open=True) is None


def test_check_cpu_reports_latest_value() -> None:
    finding = check_cpu([LOW, 91.0, 92.0, 93.0], event_open=False)
    assert finding is not None
    assert finding.type == "cpu_high"
    assert finding.value == 93.0


@pytest.mark.parametrize(
    ("samples", "fires"),
    [
        ([], False),
        ([LOW], False),
        ([HIGH], True),  # new host that starts above the threshold
        ([LOW, HIGH], True),  # crossed above
        ([HIGH, HIGH], False),  # still above: already reported
        ([HIGH, LOW], False),
        ([LOW, 90.0], False),  # exactly at the threshold is not above
    ],
)
def test_check_memory(samples: list[float], fires: bool) -> None:
    assert (check_memory(samples) is not None) == fires


def test_memory_percent() -> None:
    assert memory_percent(500, 1000) == 50.0
    assert memory_percent(1, 0) == 0.0  # no division by zero


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_check_silence_at_limit_does_not_fire() -> None:
    assert check_silence(NOW - timedelta(seconds=30), NOW) is None


def test_check_silence_after_limit_fires() -> None:
    finding = check_silence(NOW - timedelta(seconds=31), NOW)
    assert finding is not None
    assert finding.type == "host_silent"
    assert finding.value == 31.0


# ---------- watcher_was_paused ----------

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("previous_run", "now", "paused"),
    [
        (None, T0, False),  # first round: nothing to compare with
        (T0, T0 + timedelta(seconds=5), False),  # the normal interval
        (T0, T0 + timedelta(seconds=15), False),  # exactly at the limit is not paused
        (T0, T0 + timedelta(seconds=16), True),
        (T0, T0 + timedelta(seconds=61), True),  # the server container was frozen 60 s
        (T0, T0 + timedelta(hours=5), True),  # the laptop slept overnight
    ],
)
def test_watcher_was_paused(previous_run: datetime | None, now: datetime, paused: bool) -> None:
    assert watcher_was_paused(previous_run, now) == paused
