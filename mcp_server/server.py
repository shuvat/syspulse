"""SysPulse MCP server: lets an LLM query monitored hosts, metrics and anomalies.

Every tool is a read-only wrapper around the SysPulse REST API, which owns the data and
the anomaly rules. compare_hosts adds a ranking that is computed here.

Runs over stdio: the MCP client (for example Claude Code) starts this script as a
subprocess and talks to it through stdin/stdout, so there is no port to open.
"""

import os
from statistics import mean
from typing import Annotated, Any, Literal

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

API_URL = os.environ.get("API_URL", "http://localhost:8000")

mcp = MCPServer("syspulse")

# One client for all calls, so the TCP connection is reused. The timeout makes a hung
# API return an error to the model instead of blocking the tool forever.
client = httpx2.Client(base_url=API_URL, timeout=5.0)

# Same limits as the REST API. Field() puts them into the tool's JSON schema, so the
# model sees the valid range before calling.
Minutes = Annotated[int, Field(ge=1, le=1440)]

Metric = Literal["cpu_percent", "mem_percent"]

# What compare_hosts sorts by: avg over the window, the peak, or the most recent sample.
RankBy = Literal["avg", "max", "latest"]

# One JSON object from the API (a sample, a host...) or one row of a ranking.
Row = dict[str, Any]


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET a REST endpoint and return its JSON; failures become a ToolError.

    A ToolError's message is shown to the model (as an error result), so it can react,
    for example by calling list_hosts after an unknown host name. Any other exception
    would reach the model only as a generic "Error executing tool".
    """
    try:
        response = client.get(path, params=params)
    except httpx2.HTTPError as exc:
        raise ToolError(
            f"SysPulse API is not reachable at {API_URL} ({exc}). Is the server running?"
        ) from exc
    if response.status_code == 404:
        raise ToolError(f"{response.json()['detail']}. Call list_hosts to see valid host names.")
    if response.status_code != 200:
        raise ToolError(f"SysPulse API returned HTTP {response.status_code} for {path}")
    return response.json()


def metric_value(sample: Row, metric: Metric) -> float:
    """The value of `metric` in one sample (mem_percent is derived from used/total)."""
    if metric == "cpu_percent":
        return sample["cpu_percent"]
    total = sample["mem_total_mb"]
    return sample["mem_used_mb"] / total * 100 if total else 0.0


def rank_hosts(
    samples_by_host: dict[str, list[Row]], metric: Metric, rank_by: RankBy = "avg"
) -> list[Row]:
    """Rank hosts by `rank_by` (avg, max or latest) of `metric`, highest first (pure, no I/O).

    `samples_by_host` maps a host name to its samples, oldest first (as the API returns
    them). Hosts without samples in the window are listed last with samples=0, so a
    silent host is visible instead of missing.
    """
    ranked = []
    silent = []
    for host, samples in samples_by_host.items():
        if not samples:
            silent.append({"host": host, "samples": 0, "avg": None, "max": None, "latest": None})
            continue
        values = [metric_value(s, metric) for s in samples]
        ranked.append({
            "host": host,
            "samples": len(values),
            "avg": round(mean(values), 1),
            "max": round(max(values), 1),
            "latest": round(values[-1], 1),
        })
    ranked.sort(key=lambda row: row[rank_by], reverse=True)
    return ranked + sorted(silent, key=lambda row: row["host"])


@mcp.tool()
def list_hosts() -> list[dict[str, Any]]:
    """List all monitored hosts with first_seen and last_seen (UTC timestamps).

    Start here to learn the host names that the other tools expect. A host whose
    last_seen is more than 30 seconds old has stopped reporting.
    """
    return api_get("/hosts")


@mcp.tool()
def get_host_metrics(host: str, minutes: Minutes = 10) -> list[dict[str, Any]]:
    """Get the raw samples of one host from the last `minutes`, oldest first.

    One sample every 2 seconds. Fields: cpu_percent (0-100, 100 = every CPU of the
    machine busy), mem_used_mb and mem_total_mb, net_rx_bytes and net_tx_bytes
    (cumulative byte counters: the difference between two samples is the traffic in
    between). Use this to explain *why* a host looks unusual, after compare_hosts or
    find_anomalies pointed at it.
    """
    return api_get(f"/hosts/{host}/metrics", {"minutes": minutes})


@mcp.tool()
def find_anomalies(minutes: Minutes = 60) -> list[dict[str, Any]]:
    """List anomalies across all hosts from the last `minutes`, newest first.

    Types: cpu_high (CPU above 90% for 3 consecutive samples), cpu_unusual (CPU at least
    3 standard deviations above this host's own recent mean for 3 samples: a change in
    behavior even below 90%), memory_high (memory use above 90%), host_silent (no data
    for more than 30 seconds). CPU and memory are relative to each container's limits.
    Each anomaly is recorded once, when the condition starts, not for every sample while
    it lasts; `value` is the measurement that triggered it.
    """
    return api_get("/anomalies", {"minutes": minutes})


@mcp.tool()
def compare_hosts(
    metric: Metric = "cpu_percent", minutes: Minutes = 10, rank_by: RankBy = "avg"
) -> list[dict[str, Any]]:
    """Rank all hosts by a metric over the last `minutes`, highest first.

    metric: cpu_percent (CPU use, 0-100) or mem_percent (memory used / total, 0-100).
    rank_by: what to sort by. "latest" (the most recent sample) answers "which host is
    loaded right now"; "max" answers "which host had a spike"; "avg" (default) answers
    "which host is loaded over the whole window". An average hides a load that started a
    minute ago, so for "right now" questions prefer "latest".
    Each row has the number of samples and the avg, max and latest value. Hosts with
    no samples in the window come last with samples=0 (they may be down).
    """
    samples_by_host = {
        host["name"]: api_get(f"/hosts/{host['name']}/metrics", {"minutes": minutes})
        for host in api_get("/hosts")
    }
    return rank_hosts(samples_by_host, metric, rank_by)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
