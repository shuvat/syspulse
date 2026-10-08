"""Data access and pure transformations for the dashboard.

No Streamlit here, so everything can be unit-tested without a UI. The dashboard is a
read-only client of the SysPulse REST API, like the MCP server.
"""

import os
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

API_URL = os.environ.get("API_URL", "http://localhost:8000")

# Same limit as the server's host_silent rule.
SILENT_AFTER = timedelta(seconds=30)

# Agents sample every 2 seconds; a longer gap between two samples of one host means the
# host (or the whole stack) was down, and the chart line must not bridge it.
MAX_SAMPLE_GAP = timedelta(seconds=10)

Row = dict[str, Any]


class ApiError(Exception):
    """The REST API could not be reached or returned an error."""


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET a REST endpoint and return its JSON; every failure becomes an ApiError."""
    url = f"{API_URL}{path}"
    try:
        # The timeout keeps the page from hanging when the API does not answer.
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ApiError(f"SysPulse API request failed: {url} ({exc})") from exc
    return response.json()


def host_status(last_seen: str, now: datetime) -> str:
    """'online' if the host reported within SILENT_AFTER, otherwise 'silent'."""
    return "online" if now - datetime.fromisoformat(last_seen) <= SILENT_AFTER else "silent"


def metrics_frame(samples_by_host: dict[str, list[Row]]) -> pd.DataFrame:
    """One row per sample: ts, host, cpu_percent, mem_percent (long format)."""
    rows = [
        {
            "ts": pd.Timestamp(s["ts"]),
            "host": host,
            "cpu_percent": s["cpu_percent"],
            "mem_percent": s["mem_used_mb"] / s["mem_total_mb"] * 100 if s["mem_total_mb"] else 0.0,
        }
        for host, samples in samples_by_host.items()
        for s in samples
    ]
    return pd.DataFrame(rows, columns=["ts", "host", "cpu_percent", "mem_percent"])


def break_gaps(metrics: pd.DataFrame, max_gap: timedelta = MAX_SAMPLE_GAP) -> pd.DataFrame:
    """Insert an empty point (NaN values) inside every gap longer than `max_gap`.

    A line chart joins consecutive points of a host, so without this a host that was down
    for 5 minutes would get a straight line across those 5 minutes, as if it had reported.
    The empty point breaks the line instead. Each host is checked against its own previous
    sample, so hosts sampling on different seconds do not affect each other.
    """
    if metrics.empty:
        return metrics
    ordered = metrics.sort_values(["host", "ts"])
    gap = ordered.groupby("host")["ts"].diff() > max_gap
    # One empty point per gap, one second after the last sample before the gap.
    breaks = ordered.loc[gap, ["host"]].assign(
        ts=ordered["ts"].shift(1)[gap] + timedelta(seconds=1)
    )
    return pd.concat([ordered, breaks]).sort_values(["host", "ts"]).reset_index(drop=True)


def hosts_frame(
    hosts: list[Row], samples_by_host: dict[str, list[Row]], now: datetime
) -> pd.DataFrame:
    """One row per host: name, status, last_seen and the latest CPU % (None if no samples)."""
    rows = []
    for host in hosts:
        samples = samples_by_host.get(host["name"], [])
        rows.append({
            "host": host["name"],
            "status": host_status(host["last_seen"], now),
            "last_seen": pd.Timestamp(host["last_seen"]),
            "cpu_percent": samples[-1]["cpu_percent"] if samples else None,
        })
    return pd.DataFrame(rows, columns=["host", "status", "last_seen", "cpu_percent"])
