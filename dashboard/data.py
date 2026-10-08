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


def chart_frame(metrics: pd.DataFrame, column: str) -> pd.DataFrame:
    """Wide format for a line chart: index = time, one column per host.

    pivot_table (not pivot) averages samples that share a timestamp: the agent's
    timestamps have one-second resolution, so two samples can land on the same second.
    """
    if metrics.empty:
        return pd.DataFrame()
    return metrics.pivot_table(index="ts", columns="host", values=column, aggfunc="mean")


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
