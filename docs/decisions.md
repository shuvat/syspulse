# Design Decisions

## Message format: newline-delimited JSON over TCP
- Each sample is one JSON object on one line, ending with "\n".
- Why: easy to parse in both C++ and Python, human-readable for debugging,
  and the newline makes it simple to split a TCP stream into messages.
- The agent serializes with `nlohmann::json::dump()` without indentation; newlines
  inside string values are escaped, so a message can never break the line framing
  (covered by a unit test).

## Transport: TCP (not UDP)
- Why: metrics must arrive in order and without loss for anomaly detection.
  TCP handles retransmission; the agent reconnects if the server goes down.
- Known limit: `send()` succeeds once data is in the kernel buffer, not when the
  server has received it. If the server dies, the sample sent just before the
  agent notices can be lost. Guaranteed delivery would need application-level
  acknowledgements; for monitoring, losing one sample is acceptable.

## Agent threading: producer/consumer queue
- One thread collects samples every 2s, another sends them.
- Why: slow network should not delay sampling. Queue is protected by
  std::mutex + std::condition_variable.

## Bounded queue that drops the oldest sample
- Capacity is 150 samples (5 minutes at 2s). When full, `push()` drops the oldest item.
- Why: while the server is down, memory must not grow without limit. For monitoring,
  recent data is more useful than old data.
- Alternative rejected: blocking the collector when the queue is full. That would
  break the fixed 2s sampling rate.

## Parsers take strings and return std::optional
- Parsing functions take the file content, not a path; only `read_file()` touches disk.
- Why: the parsers can be unit-tested with fixed strings, no real `/proc` needed.
- They return `std::nullopt` on malformed input instead of throwing: bad input is an
  expected case, and the collector simply skips that sample.

## Metric definitions
- **CPU %** is computed from the difference between two `/proc/stat` readings, because the
  counters are cumulative since boot. Inside a container the agent reads its cgroup instead
  (see "Per-container CPU from cgroup v2" below). Idle time is `idle + iowait`.
  The math uses `double` and clamps to 0-100, because `iowait` can go backwards on Linux
  and unsigned subtraction would wrap around.
- **Memory used** is `MemTotal - MemAvailable`, not `MemTotal - MemFree`. `MemFree` ignores
  page cache that the kernel can reclaim, so memory would always look almost full.
- **Network** counters are summed over all interfaces except `lo` (loopback traffic never
  leaves the machine). They are sent as cumulative values; rates are computed by the server.

## Shutdown: StopFlag + sigwait instead of a signal handler
- `SIGINT`/`SIGTERM` are blocked in every thread and received by `main` with `sigwait()`.
- Why: a signal handler may only call async-signal-safe functions. Locking a mutex or
  notifying a condition variable from a handler can deadlock if the interrupted thread
  holds the same mutex. With `sigwait` the signal arrives as a normal event in a normal thread.
- Threads wait with `StopFlag::wait_for()` instead of `sleep_for()`, so they wake up
  immediately on shutdown instead of finishing a 2s (or 30s backoff) sleep.
- Shutdown order: stop collector, join it, close the queue, join the sender. The sender
  drains the remaining samples before exiting.

## Reconnect with exponential backoff
- Delay starts at 1s and doubles up to 30s; it resets after a successful connect.
- Why: retrying in a tight loop wastes CPU and floods a server that is restarting.
- The failed sample is resent after reconnecting, not skipped.
- `connect()` has a 5s timeout (`SO_SNDTIMEO`, which Linux also applies to connect).
  Without it, connecting to an address that never answers can block for about two minutes.
- Sends use `MSG_NOSIGNAL`, so a closed connection returns `EPIPE` instead of raising
  `SIGPIPE`, which would kill the process.

## C++ dependencies via FetchContent, pinned to releases
- nlohmann/json v3.12.0 and GoogleTest v1.17.0 (gmock disabled).
- Why: no system packages needed, and every build (local, CI, Docker) uses the same versions.
- `gtest_discover_tests` runs in `PRE_TEST` mode, so a test binary that cannot start
  (for example a sanitizer build) fails the test run, not the build.

## Database: PostgreSQL
- The data is relational (hosts -> metrics, hosts -> anomalies) with a fixed schema, and the
  main queries are time windows and joins ("samples of host X in the last 10 minutes",
  "anomalies with their host name"). SQL with foreign keys and indexes fits this directly.
- Alternative rejected: MongoDB. Flexible documents are not needed for a fixed message
  format, and joins and constraints would move into application code.
- Timestamps are `TIMESTAMP WITH TIME ZONE` (stored as UTC), not Unix integers, so time-window
  queries are plain SQL comparisons and values are readable in `psql`.
- Network counters are `BIGINT`: cumulative byte counters pass the 2^31 limit of `INTEGER`
  after about 2 GB of traffic.
- Composite index on `metrics(host_id, ts)`: matches the per-host time-window query.
  `host_id` comes first because the query filters on it by equality.
- Known limit: tables are created with `create_all`, which never alters existing tables.
  A real deployment would use migrations (Alembic).

## Server: one process for ingest and REST API
- FastAPI's `lifespan` starts the TCP ingest server and the silence watcher at startup and
  stops them at shutdown. One container, one event loop, one database engine.
- Ingest and API stay in separate modules, so they could run as separate processes later
  (write and read load grow differently).

## Ingest: validate at the boundary with Pydantic (strict mode)
- Everything from the network is untrusted input. `parse_line` is the single place that
  decides what enters the system; the rest of the code can assume valid data.
- Strict mode rejects type coercion (`"42"` is not accepted as `42`): a message that does not
  match the protocol is rejected, not guessed.
- Like the agent's parsers, `parse_line` returns `None` on bad input instead of raising.

## Ingest: blocking database calls in a worker thread
- The database code is synchronous (SQLModel/SQLAlchemy). Calling it directly in a coroutine
  would block the event loop, and with it every agent connection and the REST API.
- `store_message` runs via `asyncio.to_thread`. Awaiting it keeps samples from one agent in order.
- REST endpoints are plain `def`, so FastAPI runs them in its thread pool for the same reason.
- Alternative rejected: an async database engine. It would need a second set of database code
  and is not needed at this load.

## Host upsert: INSERT ... ON CONFLICT DO UPDATE ... RETURNING
- One atomic statement instead of SELECT followed by INSERT. With SELECT + INSERT, two
  connections announcing the same new host could both find nothing and both insert.
- Side effect: every conflicting INSERT still consumes a sequence value, so host ids have gaps.
  This is normal; sequences never promise consecutive numbers.

## last_seen uses the server clock
- `metrics.ts` is the agent's measurement time. `hosts.last_seen` answers "when did the
  server last hear from this host", so it uses the server clock and does not depend on the
  agent's clock being correct. The silence rule relies on it.

## API response models separate from the tables
- Endpoints return `HostOut`, `MetricOut` and `AnomalyOut`, not the table models.
- Why: internal ids are not exposed, anomalies carry the host name instead of `host_id`, and a
  table change does not silently change the API used by the dashboard and the MCP server.

## Anomaly rules: pure functions, edge-triggered
- The rules take lists of values (and `now` for silence) and return a finding or `None`; no
  database or real clock. They are tested directly with fixed inputs.
- Edge-triggered: one anomaly when a condition starts, not one per sample while it lasts. A
  5-minute CPU spike gives one anomaly, not 150 (avoids alert fatigue). To detect the start,
  the CPU rule looks at one sample before the streak.
- CPU and memory rules run inside `store_message`, in the same transaction as the sample.
- `host_silent` cannot be triggered by a sample, because silence means no samples arrive.
  A background task checks every 5 seconds. It records one anomaly per silence: it skips a
  host that already has a `host_silent` anomaly newer than its `last_seen`. No extra state column.
- Known limits, found in the MCP demo ([mcp_demo.md](mcp_demo.md)), not fixed yet:
  - No hysteresis: a short dip below 90% ends a CPU event, so one 2-minute load produced
    4 `cpu_high` anomalies (flapping). Fix: end the event only after CPU stays below a lower
    threshold (e.g. 80%) for several samples, or a cooldown per host.
  - The silence watcher cannot tell "the host was silent" from "the monitor itself was
    suspended" (laptop sleep froze the whole WSL VM): false `host_silent` after wake-up.
    Fix: skip a round when the watcher's own previous run is much older than 5 seconds.

## Tests against a real PostgreSQL database
- The code uses PostgreSQL-specific features (`ON CONFLICT`, `timestamptz`), so SQLite would
  test different behavior than production.
- Tests use a separate `syspulse_test` database, emptied before each test. The cost is a
  dependency on a running PostgreSQL; CI will provide it as a service container.

## Docker images: multi-stage for the agent, single stage for the server
- Agent: the build stage (`ubuntu:24.04` + build-essential, CMake) compiles the binary; the
  runtime stage is a clean `ubuntu:24.04` with only the binary copied in (118MB). Compilers
  and sources never reach the running image: smaller, and less to attack.
- Tests are not built in the image (`-DBUILD_TESTING=OFF`); they run in CI instead.
- Server: one stage on `python:3.12-slim`, because nothing is compiled (`psycopg[binary]`
  ships libpq in its wheel). `requirements.txt` is copied and installed before `app/`, so a
  code change does not reinstall the dependencies (layer cache).
- `.dockerignore` keeps the venv, tests and caches out of the build context.
- Both images run as a non-root system user: reading `/proc`, connecting and listening on
  ports above 1024 need no root.
- Exec form (`CMD ["..."]`, not a shell string): the process is PID 1 and receives the
  `SIGTERM` from `docker stop` directly, so the graceful shutdown code actually runs.
  With a shell in between, the signal would go to the shell and the process would be killed
  after 10 seconds.

## Docker Compose: readiness, ports and naming
- Services reach each other by service name (`db`, `server`) through Compose's internal DNS.
  This is why `DATABASE_URL` uses `db` and not `localhost` (inside a container, `localhost`
  is the container itself), and why the agent resolves `SERVER_HOST` with `getaddrinfo`.
- `depends_on` with `condition: service_healthy`: plain `depends_on` only waits until a
  container is *started*, not until the service inside is *ready*. Chain: `db` (`pg_isready`)
  -> `server` (`GET /health`, which also checks the database) -> agents.
- The server healthcheck uses `python -c "urllib.request.urlopen(...)"`, because the slim
  image has no curl; `urlopen` raises on a 503, so the exit code is non-zero.
- Published ports: only `127.0.0.1:8000` (API) and `127.0.0.1:5432` (database, for local
  development), bound to localhost. Port 9000 is not published: only the agents use it,
  over the internal network. Less exposed surface.
- `restart: unless-stopped` on the server and the agents. The agents also reconnect on their
  own, so a server restart needs no agent restart.
- The three agents share one definition through a YAML anchor (`x-agent`). The `environment`
  block has its own anchor, because the merge key `<<` is shallow: without it, an agent's
  `environment` would replace the shared variables instead of adding `AGENT_NAME`.

## Per-container CPU from cgroup v2 (CPU_SOURCE)
- Containers share the host kernel. Namespaces limit what a process sees, but `/proc/stat`
  and `/proc/meminfo` are not namespaced: inside a container they describe the whole machine.
  `/proc/net/dev` is per network namespace, so network counters were already per container.
- Per-container CPU accounting lives in the container's cgroup: `/sys/fs/cgroup/cpu.stat`,
  field `usage_usec` (the same source `docker stats` uses). With cgroup v2 the cgroup
  namespace is private by default, so that path is the container's own cgroup.
- `CPU_SOURCE=cgroup` (set in docker-compose.yml): `cpu% = delta usage_usec /
  (elapsed wall time x CPU capacity) x 100`, elapsed time from `steady_clock`. The capacity is
  the container's CPU limit from `/sys/fs/cgroup/cpu.max`, or every CPU of the machine if it
  has no limit (see the next section). 100 = the container uses everything it may use.
  `docker stats` uses another scale (200% = two busy cores).
- `CPU_SOURCE=proc` stays the default, so the agent on a bare machine behaves as before.
  Explicit config, not auto-detection, so behavior is predictable.
- Why: with `/proc/stat` a load in one container raised the CPU of all three agents, so
  "which host is overloaded?" had no meaningful answer.
- Known limit: memory still comes from `/proc/meminfo` (whole machine).

## CPU percent relative to the container's CPU limit
- Problem found while writing the integration test: under the same load, agent-1 reported
  92-98% one day and 49-98% the next, so the `cpu_high` rule (3 samples > 90%) fired only
  sometimes. Measured against the *whole machine*, a container reaches 90% only if nothing
  else needs CPU: on a laptop the WSL VM's virtual CPUs are shared with Windows by the
  hypervisor, and a CI runner is busy with Docker and the other containers. The test
  depended on something the system does not control (a flaky test).
- Fix: every agent gets a CPU limit in Compose (`cpus: "1.0"`), and the agent reports CPU
  relative to that limit, read from `cpu.max` (`"<quota> <period>"`, e.g. `100000 100000` =
  1 CPU; `max` = no limit). A busy container now reaches ~100% as long as one CPU of the
  machine is free. Measured: 97.6-100% under load; 4 of 4 integration runs passed.
- Also the more useful meaning for monitoring: "this host uses everything it was given" is
  what Kubernetes and `docker stats` compare against limits.
- Rejected alternative: a lower threshold in the integration test only. Smaller change, but
  the test would not use the production configuration, and the metric would still depend
  on the neighbors.
- Details: `cpu.max` is read on every sample, because a limit can change at runtime
  (`docker update --cpus`). A missing file (root cgroup, e.g. a CI runner without Docker)
  means no limit. A limit above the number of CPUs is capped at the number of CPUs, because
  it can never be reached. Not using the limit as a *denominator* when the machine itself is
  overloaded: then the container can be below 100% of its limit while starved; CPU pressure
  (`cpu.stat` `throttled_usec`, PSI) would show that, not implemented.

## Load test without stress-ng in the image
- The load must run inside agent-1, because agent-1 measures only its own cgroup.
- Installing stress-ng in the agent image grew it from 118MB to 457MB: on Ubuntu it depends
  on `libegl1`/`libgbm1` (GPU stressors), which pull in Mesa and LLVM.
- `scripts/load_test.sh` instead starts one shell busy loop per CPU in agent-1 (what
  `stress-ng --cpu 0` does for our purpose). No new dependency, image stays at 118MB.
- Measured during the load at first: 92-98%, close to the 90% threshold; the next day
  49-98% and the test failed. Fixed by measuring CPU relative to the container's limit (see
  "CPU percent relative to the container's CPU limit"): now 97.6-100%.

## AI integration: MCP server
- Why: an LLM can answer open questions ("which host is overloaded and why?") by choosing
  and combining tools, instead of a fixed dashboard view. MCP is the standard protocol for
  exposing tools to LLM clients, so the same server works with any MCP client.
- Tools, not resources: a *tool* is called by the model with arguments; a *resource* is data
  the client application chooses to load, by URI. Our functions take parameters (host,
  minutes, metric) and the model decides when to call them, so they are tools.
- The MCP server calls the REST API instead of the database: one owner of the data and the
  rules, no database credentials in the MCP server, and the API is already tested.
- `compare_hosts` has no endpoint; it combines `/hosts` and per-host metrics. Fine for 3
  hosts (N+1 requests); for many hosts a server-side aggregate endpoint would be better.
- SDK: `mcp` 2.3.0 (official). In 2.x FastMCP was renamed `MCPServer`
  (`mcp.server.fastmcp` raises an error pointing to the migration guide). Most online
  examples are 1.x. It brings `httpx2`, used as the HTTP client, so no extra dependency.
- Transport: stdio. The client starts the server as a subprocess: no port, no auth needed
  for a local tool. A shared deployment would use Streamable HTTP with authentication.
- Error handling: expected failures raise `ToolError`, so the model sees the message and can
  recover. Any other exception reaches the model only as "Error executing tool" (no
  internal details leak).
- Known limits: `mem_percent` is the same for all agents, because memory still comes from
  `/proc/meminfo` (whole machine); the cgroup's `memory.current` would fix it, like CPU.
  `compare_hosts` ranks by the average over the window, which dilutes a load that started a
  minute ago (seen in the demo: avg 4.6%, max 98.5%); ranking by a recent value would fit
  "overloaded now" better.
- Demo and what it revealed: [mcp_demo.md](mcp_demo.md). The model's explanation of an old
  `host_silent` anomaly was a wrong guess: tool results are facts, the model's interpretation
  is a hypothesis to check.

## MCP tests: mock the REST API at the HTTP layer
- The tests replace only the HTTP transport of the client (`httpx2.MockTransport`), not
  `api_get` or the tools. All our code still runs: URLs, query parameters, status-code
  handling. Mocking `api_get` would skip most of the logic under test.
- No new mocking library: `MockTransport` ships with `httpx2`. `monkeypatch` swaps the
  client for one test and restores it afterwards.
- Tools are called through `mcp.call_tool`, the path a real client uses, so the SDK's
  argument validation (from the type hints) is tested too, including "rejected before any
  API call".
- `call_tool` is async; each test runs it with `asyncio.run` instead of adding
  `pytest-asyncio`.
- Checked that the tests catch bugs by planting two (reversed sort, `minutes` not sent):
  each one failed exactly the test meant to catch it (manual mutation testing).

## CI: GitHub Actions with parallel jobs
- One job per component (`cpp`, `server`, `mcp-server`) plus `lint`, all in parallel: faster
  than one long job, and the failing job names the broken part.
- The server tests need PostgreSQL, so the `server` job runs `postgres:17` as a *service
  container* with a healthcheck; the job starts only when the database is ready (the same idea
  as `depends_on: service_healthy` in Compose). Same database engine as production, as locally.
- Clean environments: each job installs only from its own requirements files, so CI also checks
  that the dependency lists are complete.
- Least privilege: `permissions: contents: read`; `concurrency` cancels a run when a newer
  commit is pushed to the same branch.
- Plus an `integration` job that runs after all the others (see "Integration test" below).
- Actions are pinned by major version (`@v7`). Pinning to a commit SHA would protect against a
  compromised tag (supply chain) at the cost of manual updates; for a portfolio project the
  major version is the usual trade-off.

## Lint: ruff
- One tool for import sorting, unused code, likely bugs (bugbear) and outdated syntax
  (pyupgrade); it replaces flake8 + isort + pyupgrade and runs in milliseconds.
- One `ruff.toml` at the repo root for both Python components, so the rules are the same
  everywhere. `src = ["server", "mcp_server"]` tells ruff where our own modules are; without it,
  `app` and `server` imports were sorted as third-party (4 false findings).
- Only `ruff check` in CI for now. `ruff format --check` would reformat existing code in one
  big diff; it can be added later in a separate, formatting-only commit.
- The one real finding was a 105-character signature in `mcp_server/server.py`; fixed with a
  type alias (`Row = dict[str, Any]`), which also made it more readable.

## Integration test: the whole system, isolated from the development stack
- `scripts/integration_test.sh` starts the real Compose stack and checks the full path:
  agents -> ingest -> PostgreSQL -> REST API -> anomaly rule -> MCP tool (through a real stdio
  MCP client). Unit tests check the parts; this checks that they work together.
- Separate Compose project (`COMPOSE_PROJECT_NAME=syspulse-it`): own containers, network and
  volume. The cleanup runs `docker compose down -v`, which would delete the development
  database if it used the same project. It uses the same ports, so the script refuses to
  start if something already answers on the API port.
- `trap ... EXIT` always cleans up, and on failure first prints the container logs: in CI they
  are the only way to see what happened inside the containers.
- In CI the `integration` job has `needs: [cpp, server, mcp-server, lint]`: it is the slowest
  job, so it runs only when the fast checks passed.
- The MCP stdio client starts the server with a minimal environment (only variables such as
  `PATH` and `HOME`), so `API_URL` is passed to it explicitly.
