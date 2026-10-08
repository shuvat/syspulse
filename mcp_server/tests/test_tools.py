"""MCP tool tests with a mocked REST API.

The REST API is replaced at the HTTP layer (httpx2.MockTransport), so each tool runs its
real code (URLs, query parameters, error handling) without a running server. Tools are
called through `mcp.call_tool`, the same path a real MCP client uses, so argument
validation against the tool schema is tested too.
"""

import asyncio
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server
from server import mcp, metric_value, rank_hosts

HOSTS = [
    {"name": "agent-1", "first_seen": "2026-10-08T05:00:00Z", "last_seen": "2026-10-08T06:00:00Z"},
    {"name": "agent-2", "first_seen": "2026-10-08T05:00:00Z", "last_seen": "2026-10-08T06:00:00Z"},
]


def sample(cpu: float, mem_used: int = 2000, mem_total: int = 8000) -> dict[str, Any]:
    return {
        "ts": "2026-10-08T06:00:00Z",
        "cpu_percent": cpu,
        "mem_used_mb": mem_used,
        "mem_total_mb": mem_total,
        "net_rx_bytes": 0,
        "net_tx_bytes": 0,
    }


class FakeApi:
    """Answers requests from a {path: (status, json)} table and records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, Any]] = {}
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body = self.routes.get(request.url.path, (404, {"detail": "not found"}))
        return httpx2.Response(status, json=body)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    client = httpx2.Client(base_url=server.API_URL, transport=httpx2.MockTransport(fake.handle))
    monkeypatch.setattr(server, "client", client)
    return fake


def call(name: str, **arguments: Any) -> Any:
    """Call a tool like an MCP client would and return its structured result."""
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert not result.is_error
    return result.structured_content["result"]


# ---------- schema ----------

def test_all_four_tools_are_registered() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == {
        "list_hosts", "get_host_metrics", "find_anomalies", "compare_hosts",
    }


def test_schema_shows_limits_and_allowed_metrics() -> None:
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    props = tools["compare_hosts"].input_schema["properties"]
    assert props["metric"]["enum"] == ["cpu_percent", "mem_percent"]
    assert props["minutes"]["minimum"] == 1
    assert props["minutes"]["maximum"] == 1440


# ---------- thin tools ----------

def test_list_hosts_returns_api_data(api: FakeApi) -> None:
    api.routes["/hosts"] = (200, HOSTS)
    assert call("list_hosts") == HOSTS


def test_get_host_metrics_passes_host_and_minutes(api: FakeApi) -> None:
    api.routes["/hosts/agent-1/metrics"] = (200, [sample(50.0)])

    assert call("get_host_metrics", host="agent-1", minutes=5) == [sample(50.0)]
    assert api.requests[0].url.params["minutes"] == "5"


def test_find_anomalies_uses_default_window(api: FakeApi) -> None:
    api.routes["/anomalies"] = (200, [])

    assert call("find_anomalies") == []
    assert api.requests[0].url.params["minutes"] == "60"


# ---------- errors the model should see ----------

def test_unknown_host_tells_model_to_list_hosts(api: FakeApi) -> None:
    api.routes["/hosts/nope/metrics"] = (404, {"detail": "host 'nope' not found"})

    with pytest.raises(ToolError, match="host 'nope' not found.*list_hosts"):
        asyncio.run(mcp.call_tool("get_host_metrics", {"host": "nope"}))


def test_api_down_gives_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("Connection refused", request=request)

    client = httpx2.Client(base_url=server.API_URL, transport=httpx2.MockTransport(refuse))
    monkeypatch.setattr(server, "client", client)

    with pytest.raises(ToolError, match="not reachable.*Is the server running"):
        asyncio.run(mcp.call_tool("list_hosts", {}))


def test_unexpected_status_is_reported(api: FakeApi) -> None:
    api.routes["/anomalies"] = (500, {"detail": "boom"})

    with pytest.raises(ToolError, match="HTTP 500"):
        asyncio.run(mcp.call_tool("find_anomalies", {}))


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("find_anomalies", {"minutes": 0}),
        ("get_host_metrics", {"host": "agent-1", "minutes": 1441}),
        ("compare_hosts", {"metric": "disk"}),
    ],
)
def test_invalid_arguments_rejected_before_api_call(
    api: FakeApi, tool: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="validation error"):
        asyncio.run(mcp.call_tool(tool, arguments))
    assert api.requests == []  # The schema check stopped it; the API was never called.


# ---------- compare_hosts ----------

def test_compare_hosts_ranks_by_average(api: FakeApi) -> None:
    api.routes["/hosts"] = (200, HOSTS)
    api.routes["/hosts/agent-1/metrics"] = (200, [sample(10.0), sample(20.0)])
    api.routes["/hosts/agent-2/metrics"] = (200, [sample(80.0), sample(90.0)])

    ranking = call("compare_hosts", minutes=5)

    assert [row["host"] for row in ranking] == ["agent-2", "agent-1"]
    assert ranking[0] == {"host": "agent-2", "samples": 2, "avg": 85.0, "max": 90.0, "latest": 90.0}
    # Every per-host request uses the same window.
    metric_requests = [r for r in api.requests if r.url.path.endswith("/metrics")]
    assert all(r.url.params["minutes"] == "5" for r in metric_requests)


# ---------- rank_hosts / metric_value (pure) ----------

def test_rank_hosts_lists_silent_hosts_last() -> None:
    ranking = rank_hosts(
        {"quiet-b": [], "busy": [sample(50.0)], "quiet-a": []},
        "cpu_percent",
    )

    assert [row["host"] for row in ranking] == ["busy", "quiet-a", "quiet-b"]
    assert ranking[1] == {"host": "quiet-a", "samples": 0, "avg": None, "max": None, "latest": None}


def test_rank_hosts_latest_is_last_sample() -> None:
    # Samples are oldest first, so "latest" is the last one, not the maximum.
    ranking = rank_hosts({"h": [sample(90.0), sample(30.0)]}, "cpu_percent")
    assert ranking[0]["latest"] == 30.0
    assert ranking[0]["max"] == 90.0


def test_rank_hosts_empty_input() -> None:
    assert rank_hosts({}, "cpu_percent") == []


def test_metric_value_mem_percent() -> None:
    assert metric_value(sample(0.0, mem_used=2000, mem_total=8000), "mem_percent") == 25.0


def test_metric_value_mem_percent_with_zero_total() -> None:
    assert metric_value(sample(0.0, mem_used=0, mem_total=0), "mem_percent") == 0.0
