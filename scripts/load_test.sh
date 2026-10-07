#!/usr/bin/env bash
# Load test: loads every CPU inside agent-1 and checks that the server records a
# cpu_high anomaly for agent-1 only.
# Requires the Compose stack to be running: docker compose up -d --build --wait
set -euo pipefail

# docker compose looks for docker-compose.yml in the current directory.
cd "$(dirname "$0")/.."

API_URL="${API_URL:-http://localhost:8000}"
STRESS_SECONDS="${STRESS_SECONDS:-30}"

# Only anomalies newer than this count, so old runs do not make the test pass.
# One second earlier, because the agent's timestamps have one-second resolution.
start_ts=$(( $(date +%s) - 1 ))

# Prints the hosts that have a cpu_high anomaly since start_ts, one per line.
cpu_high_hosts() {
    curl -sf "$API_URL/anomalies?minutes=5" | python3 -c '
import json, sys
from datetime import datetime
start = float(sys.argv[1])
hosts = {
    a["host"] for a in json.load(sys.stdin)
    if a["type"] == "cpu_high" and datetime.fromisoformat(a["ts"]).timestamp() >= start
}
print("\n".join(sorted(hosts)))
' "$start_ts"
}

if ! curl -sf "$API_URL/health" > /dev/null; then
    echo "FAIL: server is not reachable at $API_URL (is the stack running?)" >&2
    exit 1
fi

# One busy loop per CPU inside agent-1, so the container uses (almost) the whole
# machine; the same as `stress-ng --cpu 0`, without adding stress-ng to the image.
# The script runs in agent-1's shell; the duration is passed as $1 (not pasted
# into the script text). Started with &, so errors still reach this terminal.
echo "Loading all CPUs in agent-1 for ${STRESS_SECONDS}s..."
docker compose exec -T agent-1 sh -c '
    for _ in $(seq "$(nproc)"); do
        timeout "$1" sh -c "while :; do :; done" &
    done
    wait' sh "$STRESS_SECONDS" &

# The rule needs 3 samples > 90% (2s apart), so expect the anomaly after ~6-8s.
for _ in $(seq "$STRESS_SECONDS"); do
    hosts=$(cpu_high_hosts)
    if grep -qx "agent-1" <<< "$hosts"; then
        if grep -qvx "agent-1" <<< "$hosts"; then
            echo "FAIL: cpu_high also reported for other hosts:" $hosts >&2
            exit 1
        fi
        echo "PASS: cpu_high anomaly recorded for agent-1 only"
        exit 0
    fi
    sleep 1
done

echo "FAIL: no cpu_high anomaly for agent-1 within ${STRESS_SECONDS}s" >&2
exit 1
