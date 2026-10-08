#!/usr/bin/env bash
# Integration test: builds and starts the whole system with Docker Compose and checks the
# full path: agents -> TCP ingest -> PostgreSQL -> REST API -> anomaly rules -> MCP tools.
#
# Runs as a separate Compose project (syspulse-it) with its own containers and volume, and
# removes them on exit, so the development stack and its data are never touched. It uses the
# same ports, so stop the development stack first: docker compose stop
set -euo pipefail

# docker compose looks for docker-compose.yml in the current directory.
cd "$(dirname "$0")/.."

# Every docker compose call below, and in load_test.sh, uses this project.
export COMPOSE_PROJECT_NAME=syspulse-it
API_URL="${API_URL:-http://localhost:8000}"
# Python with the mcp package installed (mcp_server/requirements.txt).
MCP_PYTHON="${MCP_PYTHON:-mcp_server/.venv/bin/python}"
HOSTS=(agent-1 agent-2 agent-3)
WAIT_SECONDS=30

if curl -s -o /dev/null "$API_URL/health"; then
    echo "FAIL: something already answers at $API_URL (stop it first: docker compose stop)" >&2
    exit 1
fi

# On any exit: print the container logs if the test failed (CI has no other way to see
# them), then remove the containers, network and volume.
cleanup() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- test failed, last container logs:" >&2
        docker compose logs --no-color --tail 40 >&2 || true
    fi
    docker compose down -v --remove-orphans > /dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT

echo "== 1. Start the stack (build, then wait for the healthchecks)"
docker compose up -d --build --wait

echo "== 2. Every agent's samples reach the database and the API"
# Prints the number of samples of host $1 from the last 5 minutes (0 if not known yet).
sample_count() {
    curl -sf "$API_URL/hosts/$1/metrics?minutes=5" \
        | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))' 2> /dev/null \
        || echo 0
}
for host in "${HOSTS[@]}"; do
    count=0
    for _ in $(seq "$WAIT_SECONDS"); do
        count=$(sample_count "$host")
        [ "$count" -gt 0 ] && break
        sleep 1
    done
    if [ "$count" -eq 0 ]; then
        echo "FAIL: no samples from $host within ${WAIT_SECONDS}s" >&2
        exit 1
    fi
    echo "   $host: $count samples"
done

echo "== 3. CPU load in agent-1 -> cpu_high anomaly for agent-1 only"
./scripts/load_test.sh

echo "== 4. MCP: compare_hosts through a real stdio MCP client ranks agent-1 first"
# The script is passed on stdin; API_URL is passed as an argument.
"$MCP_PYTHON" - "$API_URL" << 'EOF'
import asyncio
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main() -> None:
    # The stdio client starts the server with a minimal environment, so API_URL is
    # passed explicitly.
    params = StdioServerParameters(
        command=sys.executable, args=["mcp_server/server.py"], env={"API_URL": sys.argv[1]}
    )
    async with Client(params) as client:
        result = await client.call_tool("compare_hosts", {"metric": "cpu_percent", "minutes": 1})
    if result.is_error:
        sys.exit(f"FAIL: compare_hosts returned an error: {result.content}")
    ranking = result.structured_content["result"]
    for row in ranking:
        print(f"   {row['host']}: avg {row['avg']}%, max {row['max']}%")
    if ranking[0]["host"] != "agent-1":
        sys.exit("FAIL: agent-1 is not ranked first")


asyncio.run(main())
EOF

echo "PASS: integration test"
