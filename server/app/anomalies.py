"""Anomaly rules as pure functions: no database and no real clock.

The rules are edge-triggered: they fire once when a condition starts, not on
every sample while it lasts, so one long CPU spike gives one anomaly.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

CPU_THRESHOLD = 90.0  # percent
CPU_STREAK = 3  # consecutive samples
MEM_THRESHOLD = 90.0  # percent
SILENCE_LIMIT = timedelta(seconds=30)

# How many recent samples the rules need: the streak plus the one before it.
SAMPLES_NEEDED = CPU_STREAK + 1


@dataclass(frozen=True)
class Finding:
    """An anomaly found by a rule, before it is stored."""

    type: str
    value: float
    message: str


def check_cpu(recent_cpu: list[float]) -> Finding | None:
    """Fire when CPU has just been above the threshold for CPU_STREAK samples.

    `recent_cpu` holds the latest samples of one host, oldest first.
    """
    if len(recent_cpu) < CPU_STREAK:
        return None
    streak = recent_cpu[-CPU_STREAK:]
    if not all(cpu > CPU_THRESHOLD for cpu in streak):
        return None
    # If the sample before the streak was also high, the streak is longer
    # than CPU_STREAK and was already reported.
    if len(recent_cpu) > CPU_STREAK and recent_cpu[-CPU_STREAK - 1] > CPU_THRESHOLD:
        return None
    return Finding(
        type="cpu_high",
        value=streak[-1],
        message=f"CPU above {CPU_THRESHOLD:g}% for {CPU_STREAK} consecutive samples "
        f"(now {streak[-1]:.1f}%)",
    )


def memory_percent(used_mb: int, total_mb: int) -> float:
    return 0.0 if total_mb == 0 else 100.0 * used_mb / total_mb


def check_memory(recent_mem_percent: list[float]) -> Finding | None:
    """Fire when memory usage crosses above the threshold.

    `recent_mem_percent` holds the latest samples of one host, oldest first.
    """
    if not recent_mem_percent or recent_mem_percent[-1] <= MEM_THRESHOLD:
        return None
    if len(recent_mem_percent) >= 2 and recent_mem_percent[-2] > MEM_THRESHOLD:
        return None  # Already above on the previous sample: already reported.
    now = recent_mem_percent[-1]
    return Finding(
        type="memory_high",
        value=now,
        message=f"Memory usage {now:.1f}% is above {MEM_THRESHOLD:g}%",
    )


def check_silence(last_seen: datetime, now: datetime) -> Finding | None:
    """Fire when a host has sent nothing for longer than SILENCE_LIMIT.

    Reporting once per silence is the caller's job: it knows whether this
    silence was already recorded.
    """
    silent_for = now - last_seen
    if silent_for <= SILENCE_LIMIT:
        return None
    seconds = silent_for.total_seconds()
    return Finding(
        type="host_silent",
        value=seconds,
        message=f"No data for {seconds:.0f} s (limit {SILENCE_LIMIT.total_seconds():.0f} s)",
    )
