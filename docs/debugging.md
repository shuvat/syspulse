# Debugging Notes

Tools and real problems found while building SysPulse.

## ThreadSanitizer (data races)

ThreadSanitizer (TSan) instruments memory accesses at compile time and reports
when two threads access the same memory without synchronization.

```bash
cd agent
cmake -S . -B build-tsan -DCMAKE_CXX_FLAGS="-fsanitize=thread -g" \
      -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=thread"
cmake --build build-tsan
setarch -R ./build-tsan/agent_tests
```

Result: all tests pass with no TSan warnings, including the queue stress test
(4 producers x 10,000 items, 1 consumer) and the TCP sender tests.

Sanity check to try: remove the `lock_guard` in `ThreadSafeQueue::push()`, rebuild
`build-tsan` and run again. TSan should report `WARNING: ThreadSanitizer: data race`
and point to the unprotected line.

### Problem: TSan crashes on startup under WSL2

```
FATAL: ThreadSanitizer: unexpected memory mapping 0x5e93d5817000-0x5e93d5825000
```

- Cause: GCC 13's TSan runtime expects a fixed memory layout. Newer kernels (here
  WSL2, Linux 6.x) use more ASLR randomization bits, so the program can be mapped
  where TSan does not expect it. This is not a bug in the agent.
- Fix: run the binary with ASLR disabled for that process only: `setarch -R <binary>`.
  (System-wide alternative: `sudo sysctl vm.mmap_rnd_bits=28`.)
- Side effect found: the build itself failed, because `gtest_discover_tests` runs the
  test binary right after linking to list the tests. Switching to
  `DISCOVERY_MODE PRE_TEST` moves that step to `ctest` time.

## Manual end-to-end test with netcat

Verifies sending, reconnect and graceful shutdown without the real server:

```bash
# Terminal 1
nc -lk 9000
# Terminal 2
AGENT_NAME=agent-1 ./agent/build/syspulse_agent
```

Stop `nc` (Ctrl+C), wait, start it again, then Ctrl+C the agent. Observed agent log:

```
sender: connected to 127.0.0.1:9000
sender: send failed, reconnecting
sender: cannot connect to 127.0.0.1:9000, retrying in 1000 ms
sender: cannot connect to 127.0.0.1:9000, retrying in 2000 ms
sender: connected to 127.0.0.1:9000
received SIGINT, shutting down
agent stopped, dropped samples: 0
```

Samples collected during the outage were queued and delivered after the reconnect.
Out of about 9 samples, 8 arrived. The missing one is most likely the first send after
`nc` died: it went into the kernel buffer without an error (see "Transport" in
[decisions.md](decisions.md)), and only the next send failed and triggered the reconnect.

Gotcha while scripting this test: `pkill -f "nc -lk 9000"` also matches the shell
running the script (its command line contains the same text). Use
`pkill -x nc -P $$` to kill only `nc` processes started by the script.

## Server: shutdown hangs while agents are connected

- Symptom (found while writing the shutdown code, confirmed with a small script): after
  `server.close()`, `await server.wait_closed()` never returns while a client is connected.
- Cause: since Python 3.12, `asyncio.Server.wait_closed()` waits until all open connections
  are closed, not only the listening socket. Agents never disconnect on their own.
- Why it matters: `docker stop` sends SIGTERM and, after 10 seconds, SIGKILL.
- Fix: `ingest.py` keeps the open connections in a set, and `stop_ingest_server()` closes them
  before `wait_closed()`. Each `handle_client()` then sees EOF and exits normally.
  Measured: shutdown with a connected agent takes about 0.2 s.

## Server: background process ignores Ctrl+C (SIGINT)

- Symptom: `python -m app.ingest &` started from a script did not stop on `kill -INT`;
  `kill -TERM` worked.
- Cause: a non-interactive shell starts background jobs with SIGINT set to "ignore", and
  Python keeps an ignored SIGINT ignored (no `KeyboardInterrupt`). Not a bug in the server.
- Lesson: stop background processes and containers with SIGTERM; that is also what
  `docker stop` sends.

## Server: "address already in use" on startup

```
OSError: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8000): address already in use
```

- Find who holds the port: `ss -ltnp 'sport = :8000'` (and `docker ps` for published ports).
  Here it was a container from another project, and later a leftover `uvicorn` process
  still holding port 9000.
- The other container stopped with exit code 137 = 128 + 9 (SIGKILL): it did not exit on
  SIGTERM within 10 seconds, so Docker killed it.
- With `--restart=always`, a stopped container comes back when Docker restarts;
  `docker update --restart=no <name>` turns that off.

## Server: samples with the same timestamp

- Several lines sent within one second have the same `ts` (this happened in the manual
  memory test). Ordering by `ts` alone leaves their order undefined, so the edge-triggered
  rules could see the samples in the wrong order.
- Prevention: the query orders by `ts DESC, id DESC`; `id` follows insertion order. Covered by
  `test_same_timestamp_keeps_arrival_order`.

## Docker: every agent reports the same CPU

- Symptom (expected from theory, confirmed by checking the files): inside a container,
  `/proc/stat` and `/proc/meminfo` show the whole machine (e.g. `mem_total_mb` was the full
  7.8 GB of the WSL VM), while `/proc/net/dev` showed only the container's own traffic
  (`net_rx_bytes: 820`). A load in one container would raise the CPU of all three agents.
- Cause: containers share the kernel; `/proc/stat` is not namespaced, `/proc/net/dev` is
  (network namespace).
- Diagnosis inside a running agent:

  ```bash
  docker compose exec agent-1 sh -c 'cat /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/cpu.max; nproc'
  ```

  `usage_usec` is the container's own CPU time; `cpu.max` = `max 100000` means no CPU limit.
- Fix: `CPU_SOURCE=cgroup` (see [decisions.md](decisions.md)). Verified with one busy loop
  in agent-1: agent-1 reported 12.5% (one CPU out of 8), agent-2 and agent-3 stayed at 0%.
  (Later the agents got a CPU limit, and CPU % became relative to it; see the flaky load test
  below.)

## Docker: stress-ng fails silently in the agent container

```
aborting: temp-path '.' must be readable and writeable
```

- Symptom: the first load test run found no anomaly, and agent-1's CPU stayed at 0%.
  The error above was hidden, because `docker compose exec -d` detaches and drops the output.
- Cause: the container runs as the non-root user `agent` with the working directory `/`,
  and stress-ng writes temporary files to the current directory by default.
- Found by running the same command in the foreground (without `-d`).
- Fix: `--temp-path /tmp`. Lesson: `exec -d` is fire-and-forget; the script now starts the
  command with `&` instead, so errors reach the terminal.

## Docker: agent image grew from 118MB to 457MB

- Symptom: adding `stress-ng` to the runtime image (for the load test) added 340MB.
- Diagnosis:

  ```bash
  docker history syspulse-agent-1                 # size per layer: the apt-get layer was 258MB
  docker compose run --rm --no-deps --entrypoint sh agent-1 \
      -c 'ls -S /usr/lib/x86_64-linux-gnu | head'  # largest libraries: libLLVM, libgallium (Mesa)
  apt-cache depends stress-ng                      # libegl1, libgbm1 -> Mesa -> LLVM
  ```

- Cause: Ubuntu's stress-ng package has GPU stressors, so it depends on the graphics stack.
  `--no-install-recommends` does not help, because these are hard dependencies.
- Fix: stress-ng removed from the image; the load test uses one shell busy loop per CPU.

## Load test: CPU anomaly appears only sometimes (flaky test)

- Symptom: the first integration test run failed at the load step ("no cpu_high anomaly
  for agent-1 within 30s"), with no error. The day before, the same load test had passed.
- Diagnosis: start the same Compose project by hand, without the automatic cleanup, run the
  load and look at the samples:

  ```bash
  export COMPOSE_PROJECT_NAME=syspulse-it
  docker compose up -d --wait && ./scripts/load_test.sh
  curl -s "localhost:8000/hosts/agent-1/metrics?minutes=2"
  ```

  Under load: `33.9, 95.1, 88.4, 63.2, 63.9, 95.5, 88.0, 70.8, ...`: never 3 samples above
  90% in a row. The day before: 92-98%.
- Cause: CPU was measured against the whole machine. The 8 CPUs of the WSL VM are virtual and
  shared with Windows by the hypervisor, so how much of "the machine" one container gets
  depends on everything else running. The test depended on something outside the system.
- Fix: a CPU limit per agent (`cpus: "1.0"`) and CPU % relative to that limit (`cpu.max`).
  Under load now 97.6-100%; the integration test passed 4 times in a row.
- Lesson: when a test fails "sometimes", find what it depends on that the code does not
  control. Lowering the threshold would have hidden the problem, not fixed it.

## MCP: setup gotchas

- `claude` was not on the PATH in WSL (only the VS Code extension is installed). The
  extension ships the CLI: `~/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude`.
- After `claude mcp add --scope project`, `claude mcp list` shows the server as
  `Pending approval`: a project `.mcp.json` comes from the repo and runs a command on the
  machine, so Claude Code runs it only after the user approves it (in a new session or `/mcp`).
- VS Code shows its own "Start" and "Add Server..." buttons on `.mcp.json`. They belong to
  VS Code's built-in chat (Copilot), not to Claude Code; no need to click them.
- To check the server without Claude Code, launch it exactly as `.mcp.json` says from the repo
  root with the SDK's client (`mcp.Client` + `StdioServerParameters`) and list the tools.

## Server: false `host_silent` after the laptop slept

- Symptom (found in the MCP demo): `host_silent` on agent-1 at 05:51, "No data for 19173 s"
  (5.3 hours), although `docker compose ps` showed all containers up for 10 hours. The model
  explained it as "the stack was off", which was wrong.
- Diagnosis: look for gaps between consecutive samples of a host (via the API):

  ```bash
  curl -s "localhost:8000/hosts/agent-2/metrics?minutes=600" | python3 -c '
  import json, sys
  from datetime import datetime
  prev = None
  for s in json.load(sys.stdin):
      t = datetime.fromisoformat(s["ts"])
      if prev and (t - prev).total_seconds() > 60: print("gap:", prev, "->", t)
      prev = t'
  ```

  All three agents had the same gaps (20:04 -> 00:31 -> 05:51): the laptop slept, so the WSL
  VM was suspended, and the agents *and* the server were frozen together.
- Why only agent-1: on wake-up, agent-2 and agent-3 sent a sample a few milliseconds before
  the watcher's next round, agent-1 did not (a race).
- Lesson: a monitor must be able to tell "the host was silent" from "I was not running".
  Not fixed yet (see [decisions.md](decisions.md)).

## Anomalies: `cpu_high` fires several times during one load

- Symptom: one 2-minute load gave 4 `cpu_high` anomalies (05:59:36, 05:59:54, 06:00:12,
  06:00:40), found with `curl -s "localhost:8000/anomalies?minutes=600"`.
- Cause: the rule is edge-triggered without hysteresis. Samples under load fluctuate around
  90-98% with occasional dips (59.9% once); every dip below 90% ends the event, and the next
  3 high samples start a new one (flapping).
- Not fixed yet (see [decisions.md](decisions.md)).

## Tests: checking that the tests catch bugs

- A passing test suite only proves something if it fails on wrong code. For the MCP tools,
  two bugs were planted one at a time and the tests were run:
  - `reverse=True` -> `reverse=False` in `rank_hosts`: `test_compare_hosts_ranks_by_average` failed.
  - `minutes` not passed to the API in `get_host_metrics`:
    `test_get_host_metrics_passes_host_and_minutes` failed.
- Then `server.py` was restored (no diff). This is manual mutation testing; tools such as
  `mutmut` automate it.

## Lint: ruff reports our own imports as unsorted

- Symptom: the first `ruff check .` from the repo root reported `I001 Import block is
  un-sorted` in 4 test files, and its suggested fix moved `from app...` imports *above*
  `from fastapi...` and `from sqlmodel...`.
- Cause: run from the repo root, ruff did not know that `app` (in `server/`) and `server`
  (in `mcp_server/`) are first-party modules, so it sorted them as third-party.
- Fix: `src = ["server", "mcp_server"]` in `ruff.toml`. Lesson: read a tool's suggested fix
  before applying `--fix`; here it would have "fixed" correct code.

## Dashboard: AppTest gotchas

- `AppTest.from_file("app.py")` failed with `FileNotFoundError: ... dashboard/tests/app.py`:
  the path is relative to the *test file* that calls it, not to the current directory.
  Fix: `AppTest.from_file("../app.py")`.
- The "API down" test found no error message on the page. Cause: `load()` is decorated with
  `@st.cache_data(ttl=4)`, and the cache key is only its arguments (`minutes=10`). The test
  ran within 4 seconds of the previous one, so it got the cached data of the fake API instead
  of calling the failing one. Fix: `st.cache_data.clear()` before each app test.
  In production this is the intended behavior; in tests, shared state between tests must be
  reset.

## GDB

*(Day 6: breakpoints, `info threads`, `thread apply all bt`, planted bug.)*

## Valgrind

*(Day 6: `valgrind --leak-check=full` on the agent.)*
