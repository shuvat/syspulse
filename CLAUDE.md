# SysPulse - Project Context for Claude Code

## Who I am and why this project exists
I'm Shuvat, a 3rd-year B.Sc. Computer Science student at Bar-Ilan University, applying for student
software positions (NVIDIA, Mobileye, SAP, HPE/Zerto, Annapurna Labs, Samsung, ELTA, etc.).
I have one week (~7 hours/day) to build this project as a portfolio piece.
It is designed to close gaps that keep appearing in job requirements:
testing + CI, AI agents / MCP, Python backend + SQL, Linux + networking + multithreading in C++,
Docker, observability, and GDB/valgrind.

## How I want to work with you (important)
- **Talk to me in Hebrew. Write all code, comments, commit messages and docs in English.**
- I must understand every line - I will be asked about this project in interviews.
  - Before writing code for a step, give a short plan (3-5 bullets) and wait for my OK.
  - After writing code, explain the key concepts briefly (why, not just what).
  - Point out things I should be able to explain in an interview.
- Work in small steps (one step from the plan at a time). Never do a whole day at once.
- After each step: tell me exactly how to verify it works, and suggest a commit message.
- Ask before adding a new dependency or changing the architecture.
- Prefer simple, readable code over clever code.
- When I ask "why", answer the question - don't just rewrite the code.

## Environment
- Windows 11 + WSL2, Ubuntu 24.04. All work happens inside WSL (`~/projects/syspulse`).
- g++ 13.3, CMake 3.28, GDB 15.1, valgrind 3.22, Python 3 (use a venv), Docker Desktop
  with WSL integration (Docker Compose v2), stress-ng installed.
- VS Code connected via WSL extension; GitHub repo: github.com/shuvat/syspulse

## Architecture
```
[C++ agent x3] --TCP, newline-delimited JSON--> [Python ingest (asyncio) + FastAPI] --> [PostgreSQL]
                                                         ^                ^
                                          [MCP server (FastMCP)]   [Streamlit dashboard]
                                                         ^
                                                 [Claude (LLM client)]
```

### Components
1. **agent/** (C++17, CMake): multithreaded Linux agent.
   - Collector thread: every 2s reads /proc/stat, /proc/meminfo, /proc/net/dev.
   - Thread-safe queue (std::mutex + std::condition_variable) between collector and sender.
   - Sender thread: TCP client, sends one JSON object per line, reconnects with backoff.
   - Graceful shutdown on SIGINT/SIGTERM.
   - Parsing functions take `std::string` input (not file paths) so they are unit-testable.
   - Libraries: nlohmann/json, GoogleTest (both via CMake FetchContent).
   - Config via env vars: SERVER_HOST, SERVER_PORT, AGENT_NAME.
2. **server/** (Python): asyncio TCP ingest on port 9000 started inside FastAPI lifespan;
   REST API on port 8000; SQLModel/SQLAlchemy + PostgreSQL; anomaly rules as pure functions.
3. **mcp_server/** (Python, official MCP SDK / FastMCP): tools that call the REST API.
4. **dashboard/** (Streamlit): hosts table, CPU/memory charts, anomalies list.
5. **docker-compose.yml**: db, server, agent-1..3, dashboard.
6. **.github/workflows/ci.yml**: C++ build+ctest, Python pytest+ruff, integration test.

### Message format
```json
{"host":"agent-1","ts":1760000000,"cpu_percent":37.5,"mem_used_mb":2048,"mem_total_mb":8192,"net_rx_bytes":123456,"net_tx_bytes":65432}
```

### Database schema
```
hosts(id, name UNIQUE, first_seen, last_seen)
metrics(id, host_id -> hosts, ts, cpu_percent, mem_used_mb, mem_total_mb, net_rx_bytes, net_tx_bytes)
anomalies(id, host_id -> hosts, ts, type, value, message)
```

### REST API
GET /health, GET /hosts, GET /hosts/{name}/metrics?minutes=10, GET /anomalies?minutes=60

### Anomaly rules
- CPU > 90% for 3 consecutive samples
- Memory usage > 90%
- Host silent for more than 30 seconds

### MCP tools
- list_hosts() -> all hosts + last_seen
- get_host_metrics(host, minutes=10) -> recent samples
- find_anomalies(minutes=60) -> anomalies across hosts
- compare_hosts(metric="cpu_percent") -> ranking of hosts

## Repo structure
```
agent/        CMakeLists.txt, src/ (main.cpp, collector.*, sender.*, queue.h, stop_flag.h), tests/
server/       app/ (main.py, ingest.py, db.py, models.py, anomalies.py), tests/, requirements.txt
mcp_server/   server.py
dashboard/    app.py
scripts/      load_test.sh, integration_test.sh
docs/         architecture.md, decisions.md, debugging.md
docker-compose.yml, .github/workflows/ci.yml, README.md
```

## Week plan (check off as we go)
### Day 1 - C++ agent
- [x] Repo + folder structure, docs/decisions.md started
- [x] Parsing functions: CPU % from /proc/stat deltas, /proc/meminfo, /proc/net/dev
- [x] GoogleTest via FetchContent + tests for each parser (ctest passes)
- [x] Thread-safe queue + collector thread + sender thread
- [x] TCP sending with reconnect; graceful Ctrl+C
- Done when: `nc -lk 9000` shows a JSON line every 2s; agent reconnects after nc restarts.

### Day 2 - Python server + PostgreSQL
- [x] PostgreSQL in a container; schema with SQLModel
- [x] asyncio TCP ingest (bad lines are logged, never crash the server)
- [x] REST endpoints + check /docs
- [x] anomalies.py (pure functions) + pytest (rules, parsing, endpoints via TestClient)

### Day 3 - Docker Compose + load test
- [x] Multi-stage Dockerfile for agent; Dockerfile for server
- [x] docker-compose.yml with healthcheck + depends_on + volume; 3 agents
- [ ] scripts/load_test.sh (stress-ng in one agent) -> CPU anomaly appears
- [ ] docs/architecture.md with a Mermaid diagram

### Day 4 - MCP server
- [ ] FastMCP server with the 4 tools (clear docstrings)
- [ ] Connect to Claude Code (`claude mcp add`), demo: "Which host is overloaded and why?"
- [ ] pytest for tools with mocked REST API

### Day 5 - CI
- [ ] GitHub Actions: cpp job, python job (pytest + ruff), integration job (needs both)
- [ ] scripts/integration_test.sh; CI badge in README

### Day 6 - Dashboard, debugging, README
- [ ] Streamlit dashboard added to compose
- [ ] valgrind --leak-check=full on agent -> no leaks
- [ ] GDB session (breakpoints, info threads, backtrace, planted bug) -> docs/debugging.md
- [ ] README: one-line pitch, demo video, diagram, tech stack, 3-command quick start

### Day 7 - Buffer, polish, interview prep
- [ ] Finish leftovers; optional statistical anomaly rule (mean + std dev)
- [ ] Update CV + LinkedIn
- [ ] Prepare answers: TCP vs UDP, mutex vs condition variable, CPU % from /proc/stat,
      PostgreSQL vs MongoDB, how MCP works (tool vs resource), what CI checks, scaling to 1000 hosts

## Current status
Day 1 complete: C++ agent works end to end (verified against `nc -lk 9000`, including reconnect
and Ctrl+C). 33 GoogleTest tests pass, clean under ThreadSanitizer.
Day 2 complete: Python server (ingest + REST + anomaly rules) works end to end with the real agent
and PostgreSQL (CPU anomaly verified with stress-ng). 52 pytest tests pass.
Day 3 in progress: Dockerfiles + full compose stack (db, server, agent-1..3) work end to end
(`docker compose up -d --build --wait`).
Next: Day 3, step 3 - scripts/load_test.sh (stress-ng in one agent).

### Agent notes (decisions made during Day 1)
- Parsers return `std::optional` (nullopt on malformed input) instead of throwing.
- Network counters are summed over all interfaces except `lo`; memory used = MemTotal - MemAvailable.
- Queue is bounded (150 samples = 5 min) and drops the oldest item when full.
- `StopFlag` (mutex + condition_variable) lets sleeping threads wake immediately on shutdown.
- SIGINT/SIGTERM are blocked in all threads and received in main via `sigwait` (no signal handler).
- Sender retries the same sample after reconnect; backoff 1s doubling to 30s. Known limit: one
  sample can be lost when the server dies (send() succeeds into the kernel buffer).
- Dependencies (pinned, FetchContent): nlohmann/json v3.12.0, GoogleTest v1.17.0 (gmock off).
- `gtest_discover_tests` uses `DISCOVERY_MODE PRE_TEST`.
- TSan on this WSL kernel needs ASLR off: `setarch -R ./build-tsan/agent_tests`.

### Server notes (decisions made during Day 2)
- Run from `server/` with the venv: `uvicorn app.main:app --port 8000`; DB: `docker compose up -d --wait db`.
  `DATABASE_URL` env var (default: localhost, user/password/db `syspulse`).
- One process: FastAPI `lifespan` starts the TCP ingest (port 9000) and the silent-host watcher.
- Dependencies (pinned): fastapi 0.142.2, uvicorn 0.54.0, sqlmodel 0.0.48, psycopg[binary] 3.3.6;
  dev (`requirements-dev.txt`): pytest 9.1.1, httpx 0.28.1. Starlette warns that httpx is
  deprecated for TestClient (suggests `httpx2`) - not switched yet.
- Schema: timestamps are `TIMESTAMP WITH TIME ZONE`; net counters are `BIGINT`; index on
  `metrics(host_id, ts)`. `create_all` only creates missing tables (no migrations): after a model
  change run `docker compose down -v`.
- Ingest: Pydantic `MetricMessage` in strict mode; `parse_line` returns None on bad input.
  DB writes run via `asyncio.to_thread`. Host upsert = `INSERT ... ON CONFLICT DO UPDATE RETURNING`.
  `last_seen` uses the server clock; metric `ts` uses the agent's.
- Shutdown: Python 3.12 `Server.wait_closed()` waits for open connections, so
  `stop_ingest_server` closes all agent connections first.
- Endpoints are plain `def` (thread pool) with separate response models (`HostOut`, ...).
- Anomaly rules are pure and edge-triggered (one anomaly per event). CPU/memory rules run inside
  `store_message` (same transaction); `host_silent` runs in a background task every 5s, once per
  silence (skip if an anomaly newer than `last_seen` exists).
- Tests use a real PostgreSQL database `syspulse_test` (created by conftest);
  `TestClient(app)` without `with`, so the lifespan does not run.
