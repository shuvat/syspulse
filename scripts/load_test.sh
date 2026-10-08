#!/usr/bin/env bash
# Load test: puts load on agent-1 and checks that the server records the matching anomaly
# for agent-1 only.
#   LOAD=cpu (default): every CPU busy -> cpu_high
#   LOAD=memory:        ~92% of agent-1's memory limit held -> memory_high
# Requires the Compose stack to be running: docker compose up -d --build --wait
set -euo pipefail

# docker compose looks for docker-compose.yml in the current directory.
cd "$(dirname "$0")/.."

API_URL="${API_URL:-http://localhost:8000}"
STRESS_SECONDS="${STRESS_SECONDS:-30}"
LOAD="${LOAD:-cpu}"

case "$LOAD" in
    cpu) ANOMALY=cpu_high ;;
    memory) ANOMALY=memory_high ;;
    *) echo "FAIL: LOAD must be cpu or memory, not '$LOAD'" >&2; exit 1 ;;
esac

# Only anomalies newer than this count, so old runs do not make the test pass.
# One second earlier, because the agent's timestamps have one-second resolution.
start_ts=$(( $(date +%s) - 1 ))

# Prints the hosts that have an $ANOMALY anomaly since start_ts, one per line.
anomaly_hosts() {
    curl -sf "$API_URL/anomalies?minutes=5" | python3 -c '
import json, sys
from datetime import datetime
anomaly_type, start = sys.argv[1], float(sys.argv[2])
hosts = {
    a["host"] for a in json.load(sys.stdin)
    if a["type"] == anomaly_type and datetime.fromisoformat(a["ts"]).timestamp() >= start
}
print("\n".join(sorted(hosts)))
' "$ANOMALY" "$start_ts"
}

if ! curl -sf "$API_URL/health" > /dev/null; then
    echo "FAIL: server is not reachable at $API_URL (is the stack running?)" >&2
    exit 1
fi

# The load runs in agent-1's shell; the duration is passed as $1 (not pasted into the
# script text). Started with &, so errors still reach this terminal.
if [ "$LOAD" = cpu ]; then
    # One busy loop per CPU of the machine (the same as `stress-ng --cpu 0`, without
    # adding stress-ng to the image). agent-1 is limited to one CPU, so the loops keep
    # it at 100% of its limit as long as one CPU of the machine is free.
    echo "Loading all CPUs in agent-1 for ${STRESS_SECONDS}s..."
    docker compose exec -T agent-1 sh -c '
        for _ in $(seq "$(nproc)"); do
            timeout "$1" sh -c "while :; do :; done" &
        done
        wait' sh "$STRESS_SECONDS" &
else
    # Hold 92% of the container's memory limit (read from memory.max, so a different
    # mem_limit still works and stays below the OOM killer). tail keeps its whole input
    # in memory (zeros have no newlines) until the input ends after `sleep`.
    echo "Holding 92% of agent-1's memory limit for ${STRESS_SECONDS}s..."
    docker compose exec -T agent-1 sh -c '
        limit=$(cat /sys/fs/cgroup/memory.max)
        if [ "$limit" = max ]; then
            echo "FAIL: agent-1 has no memory limit (mem_limit in docker-compose.yml)" >&2
            exit 1
        fi
        { head -c $((limit * 92 / 100)) /dev/zero; sleep "$1"; } | tail > /dev/null' \
        sh "$STRESS_SECONDS" &
fi

# The CPU rule needs 3 samples > 90% (2s apart), so expect it after ~6-8s; the memory
# rule fires on the first sample above 90%.
for _ in $(seq "$STRESS_SECONDS"); do
    hosts=$(anomaly_hosts)
    if grep -qx "agent-1" <<< "$hosts"; then
        if grep -qvx "agent-1" <<< "$hosts"; then
            echo "FAIL: $ANOMALY also reported for other hosts:" $hosts >&2
            exit 1
        fi
        echo "PASS: $ANOMALY anomaly recorded for agent-1 only"
        exit 0
    fi
    sleep 1
done

echo "FAIL: no $ANOMALY anomaly for agent-1 within ${STRESS_SECONDS}s" >&2
exit 1
