"""Pure rule tests: no database, no real clock."""

from datetime import UTC, datetime, timedelta

import pytest

from app.anomalies import check_cpu, check_memory, check_silence, memory_percent

HIGH = 95.0
LOW = 10.0


@pytest.mark.parametrize(
    ("samples", "fires"),
    [
        ([], False),
        ([HIGH, HIGH], False),  # streak too short
        ([HIGH, HIGH, HIGH], True),  # new host whose first samples are high
        ([LOW, HIGH, HIGH, HIGH], True),  # streak just reached 3
        ([HIGH, HIGH, HIGH, HIGH], False),  # streak continues: already reported
        ([LOW, HIGH, LOW, HIGH], False),  # not consecutive
        ([HIGH, HIGH, HIGH, LOW], False),  # streak ended
        ([LOW, 90.0, 90.0, 90.0], False),  # exactly at the threshold is not above
        ([LOW, 90.1, 90.1, 90.1], True),
    ],
)
def test_check_cpu(samples: list[float], fires: bool) -> None:
    assert (check_cpu(samples) is not None) == fires


def test_check_cpu_reports_latest_value() -> None:
    finding = check_cpu([LOW, 91.0, 92.0, 93.0])
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
