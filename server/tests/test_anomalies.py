"""Pure rule tests: no database, no real clock."""

from datetime import UTC, datetime, timedelta

import pytest

from app.anomalies import (
    Finding,
    check_cpu,
    check_cpu_unusual,
    check_memory,
    check_silence,
    cpu_baseline,
    cpu_event_open,
    memory_percent,
    unusual_event_open,
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


# ---------- statistical rule: cpu_unusual ----------

# A quiet host: 40 samples around 5% (small noise), i.e. more than the 30 needed.
QUIET = [4.0, 5.0, 6.0, 5.0] * 10


def unusual(recent: list[float], event_open: bool = False) -> Finding | None:
    return check_cpu_unusual(recent, cpu_baseline(recent), event_open)


def test_unusual_fires_on_jump_that_threshold_rule_misses() -> None:
    recent = QUIET + [60.0, 60.0, 60.0]

    finding = unusual(recent)

    assert finding is not None
    assert finding.type == "cpu_unusual"
    assert "above this host's recent mean (5.0%)" in finding.message
    # The fixed threshold rule stays quiet: 60% is not "high".
    assert check_cpu(recent, event_open=False) is None


def test_unusual_needs_three_consecutive_samples() -> None:
    assert unusual(QUIET + [60.0, 60.0]) is None  # only 2 high samples
    assert unusual(QUIET + [60.0, 5.0, 60.0]) is None  # not consecutive


def test_unusual_ignores_noisy_host() -> None:
    # A host that swings between 20% and 80% (std 30): 90% is only 1.3 std devs above.
    noisy = [20.0, 80.0] * 20
    assert unusual(noisy + [90.0, 90.0, 90.0]) is None


def test_unusual_std_floor_protects_constant_host() -> None:
    # Always exactly 0%: std is 0, floored to 5 points.
    flat = [0.0] * 40
    assert unusual(flat + [3.0, 3.0, 3.0]) is None  # 0.6 floored std devs: noise
    assert unusual(flat + [20.0, 20.0, 20.0]) is not None  # 4 floored std devs


def test_unusual_needs_enough_history() -> None:
    # A new host: 20 samples of baseline are not enough to judge.
    assert cpu_baseline([5.0] * 20 + [60.0, 60.0, 60.0]) is None
    assert unusual([5.0] * 20 + [60.0, 60.0, 60.0]) is None


def test_unusual_reports_only_rises() -> None:
    assert unusual([60.0] * 40 + [0.0, 0.0, 0.0]) is None


def test_unusual_quiet_while_event_open() -> None:
    assert unusual(QUIET + [60.0, 60.0, 60.0], event_open=True) is None


def test_baseline_excludes_the_samples_being_judged() -> None:
    avg, _ = cpu_baseline(QUIET + [60.0, 60.0, 60.0])
    assert avg == 5.0


@pytest.mark.parametrize(
    ("cpu_since_alert", "is_open"),
    [
        (None, False),  # never had a cpu_unusual anomaly
        ([60.0, 60.0, 60.0], True),  # still far from the 5% baseline
        ([60.0, 5.0, 5.0], True),  # only 2 samples back to normal
        ([60.0, 5.0, 6.0, 4.0], False),  # 3 samples within 1 std dev: ended
    ],
)
def test_unusual_event_open(cpu_since_alert: list[float] | None, is_open: bool) -> None:
    assert unusual_event_open(cpu_since_alert, (5.0, 5.0)) == is_open

