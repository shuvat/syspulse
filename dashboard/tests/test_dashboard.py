"""Dashboard tests: pure data functions, plus a smoke test of the whole app.

The smoke test uses Streamlit's AppTest, which runs app.py without a browser. The REST API
is replaced by monkeypatching data.api_get, so no server is needed.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
import requests
import streamlit as st
from streamlit.testing.v1 import AppTest

import data

NOW = datetime(2026, 10, 8, 6, 0, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def sample(ts: str, cpu: float, mem_used: int = 2000, mem_total: int = 8000) -> dict[str, Any]:
    return {
        "ts": ts, "cpu_percent": cpu, "mem_used_mb": mem_used, "mem_total_mb": mem_total,
        "net_rx_bytes": 0, "net_tx_bytes": 0,
    }


# ---------- host_status ----------

def test_host_is_online_within_limit() -> None:
    assert data.host_status(iso(NOW - timedelta(seconds=30)), NOW) == "online"


def test_host_is_silent_after_limit() -> None:
    assert data.host_status(iso(NOW - timedelta(seconds=31)), NOW) == "silent"


# ---------- metrics_frame / chart_frame ----------

def test_metrics_frame_computes_mem_percent() -> None:
    frame = data.metrics_frame({"a": [sample("2026-10-08T06:00:00Z", 10.0, 2000, 8000)]})
    assert frame.loc[0, "mem_percent"] == 25.0
    assert frame.loc[0, "host"] == "a"


def test_metrics_frame_zero_total_memory() -> None:
    frame = data.metrics_frame({"a": [sample("2026-10-08T06:00:00Z", 10.0, 0, 0)]})
    assert frame.loc[0, "mem_percent"] == 0.0


# ---------- break_gaps ----------

def series(frame: Any, host: str) -> list[float]:
    """The cpu_percent values of one host in time order (NaN = a line break)."""
    return frame[frame["host"] == host].sort_values("ts")["cpu_percent"].tolist()


def test_break_gaps_misaligned_hosts_stay_continuous() -> None:
    # The case found in a screenshot: under load agent-1 drifted to even seconds while the
    # others stayed on odd seconds. Each host's own series must have no NaN.
    frame = data.metrics_frame({
        "agent-1": [sample(f"2026-10-08T08:31:{s:02d}Z", 100.0) for s in (0, 2, 4, 6)],
        "agent-2": [sample(f"2026-10-08T08:31:{s:02d}Z", 0.0) for s in (1, 3, 5, 7)],
    })

    result = data.break_gaps(frame)

    assert series(result, "agent-1") == [100.0] * 4
    assert series(result, "agent-2") == [0.0] * 4


def test_break_gaps_inserts_one_break_in_a_long_gap() -> None:
    frame = data.metrics_frame({"a": [
        sample("2026-10-08T06:00:00Z", 10.0),
        sample("2026-10-08T06:00:02Z", 20.0),
        sample("2026-10-08T06:01:02Z", 30.0),  # 60 s later: the host was down.
    ]})

    values = series(data.break_gaps(frame), "a")

    assert len(values) == 4
    assert values[:2] == [10.0, 20.0]
    assert pd.isna(values[2])  # The break, right after the last sample before the gap.
    assert values[3] == 30.0


def test_break_gaps_normal_interval_has_no_break() -> None:
    frame = data.metrics_frame({"a": [
        sample("2026-10-08T06:00:00Z", 10.0), sample("2026-10-08T06:00:02Z", 20.0),
    ]})
    assert series(data.break_gaps(frame), "a") == [10.0, 20.0]


def test_break_gaps_empty() -> None:
    assert data.break_gaps(data.metrics_frame({})).empty


# ---------- hosts_frame ----------

def test_hosts_frame_status_and_latest_cpu() -> None:
    hosts = [
        {"name": "a", "first_seen": iso(NOW), "last_seen": iso(NOW)},
        {"name": "b", "first_seen": iso(NOW), "last_seen": iso(NOW - timedelta(minutes=5))},
    ]
    samples = {"a": [sample(iso(NOW), 10.0), sample(iso(NOW), 75.0)], "b": []}

    frame = data.hosts_frame(hosts, samples, NOW)

    assert frame["status"].tolist() == ["online", "silent"]
    assert frame.loc[0, "cpu_percent"] == 75.0  # The latest sample, not the first.
    assert frame["cpu_percent"].isna().tolist() == [False, True]


# ---------- api_get ----------

def test_api_get_wraps_connection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise requests.ConnectionError("Connection refused")

    monkeypatch.setattr(data.requests, "get", refuse)

    with pytest.raises(data.ApiError, match="request failed"):
        data.api_get("/hosts")


# ---------- the whole app (AppTest) ----------

def fake_api(path: str, params: dict[str, Any] | None = None) -> Any:
    now = datetime.now(UTC)
    if path == "/hosts":
        return [{"name": "agent-1", "first_seen": iso(now), "last_seen": iso(now)}]
    if path == "/hosts/agent-1/metrics":
        return [sample(iso(now - timedelta(seconds=2)), 40.0), sample(iso(now), 95.0)]
    if path == "/anomalies":
        return [{"host": "agent-1", "ts": iso(now), "type": "cpu_high", "value": 95.0,
                 "message": "CPU above 90% for 3 consecutive samples (now 95.0%)"}]
    raise AssertionError(f"unexpected path {path}")


def run_app(monkeypatch: pytest.MonkeyPatch, api: Any) -> AppTest:
    monkeypatch.setattr(data, "api_get", api)
    # load() is cached by its arguments only, so without this a test would get the
    # result cached by the previous test instead of calling the fake API.
    st.cache_data.clear()
    # Relative to this test file, not to the current directory.
    app = AppTest.from_file("../app.py", default_timeout=10)
    app.run()
    return app


def test_app_renders_hosts_charts_and_anomalies(monkeypatch: pytest.MonkeyPatch) -> None:
    app = run_app(monkeypatch, fake_api)

    assert not app.exception
    assert [s.value for s in app.subheader] == [
        "Hosts", "CPU %", "Memory %", "Anomalies (last 60 minutes)",
    ]
    hosts_table = app.dataframe[0].value
    assert hosts_table["host"].tolist() == ["agent-1"]
    assert hosts_table["status"].tolist() == ["🟢 online"]
    assert app.dataframe[1].value["type"].tolist() == ["cpu_high"]


def test_app_shows_error_when_api_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(path: str, params: dict[str, Any] | None = None) -> Any:
        raise data.ApiError("SysPulse API request failed: http://x/hosts (refused)")

    app = run_app(monkeypatch, down)

    assert not app.exception  # An error message, not a crash.
    assert "Is the server running?" in app.error[0].value
