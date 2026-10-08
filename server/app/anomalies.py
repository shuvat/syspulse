"""Anomaly rules as pure functions: no database and no real clock.

The rules are edge-triggered: they fire once when a condition starts, not on
every sample while it lasts, so one long CPU spike gives one anomaly. The CPU rule
also has hysteresis: an event starts above CPU_THRESHOLD but ends only after CPU
stays below the lower CPU_CLEAR_THRESHOLD, so short dips do not split one event
into many (flapping).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

CPU_THRESHOLD = 90.0  # percent: a CPU event starts above this...
CPU_STREAK = 3  # ...for this many consecutive samples.
CPU_CLEAR_THRESHOLD = 80.0  # percent: the event ends below this...
CPU_CLEAR_STREAK = 3  # ...for this many consecutive samples.
MEM_THRESHOLD = 90.0  # percent
SILENCE_LIMIT = timedelta(seconds=30)
# The silence watcher runs every 5 s. Much more wall-clock time between two of its rounds
# means the watcher itself was not running (machine asleep, VM or container frozen).
WATCHER_PAUSE_LIMIT = timedelta(seconds=15)

# How many recent samples the caller loads (5 minutes at 2 s). A new CPU event can only
# start right after the previous one cleared, so the clearing run is always in this window.
SAMPLES_NEEDED = 150


@dataclass(frozen=True)
class Finding:
    """An anomaly found by a rule, before it is stored."""

    type: str
    value: float
    message: str


def cpu_event_open(cpu_since_alert: list[float] | None) -> bool:
    """Whether the host's last cpu_high event is still going on.

    `cpu_since_alert` holds the CPU samples after the host's last cpu_high anomaly,
    oldest first, or None if the host never had one. The event ends once CPU has been
    below CPU_CLEAR_THRESHOLD for CPU_CLEAR_STREAK consecutive samples.
    """
    if cpu_since_alert is None:
        return False
    run = 0
    for cpu in cpu_since_alert:
        run = run + 1 if cpu < CPU_CLEAR_THRESHOLD else 0
        if run >= CPU_CLEAR_STREAK:
            return False
    return True


def check_cpu(recent_cpu: list[float], event_open: bool) -> Finding | None:
    """Fire when CPU is above the threshold for CPU_STREAK samples and no event is open.

    `recent_cpu` holds the latest samples of one host, oldest first. `event_open` comes
    from cpu_event_open(): while an event is open, the rule stays quiet, so a long or
    unsteady spike gives one anomaly.
    """
    if event_open or len(recent_cpu) < CPU_STREAK:
        return None
    streak = recent_cpu[-CPU_STREAK:]
    if not all(cpu > CPU_THRESHOLD for cpu in streak):
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


def watcher_was_paused(previous_run: datetime | None, now: datetime) -> bool:
    """Whether the silence watcher was not running between its previous round and now.

    Wall-clock time on purpose: during a machine sleep, Linux's monotonic clock stops,
    while the wall clock jumps forward, and that jump is the signal.
    """
    return previous_run is not None and now - previous_run > WATCHER_PAUSE_LIMIT


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
