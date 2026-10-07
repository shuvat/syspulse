# Architecture

SysPulse collects system metrics from Linux hosts, stores them in PostgreSQL,
detects anomalies, and exposes the data to a dashboard and to an LLM through MCP.

> Status: the C++ agent and the Python server are implemented. The MCP server,
> the dashboard and Docker Compose for the full system are planned (marked *planned* below).

## Overview

```mermaid
flowchart LR
    subgraph hosts[Monitored hosts]
        A1[agent-1<br/>C++]
        A2[agent-2<br/>C++]
        A3[agent-3<br/>C++]
    end

    subgraph server[Python server]
        I[asyncio TCP ingest<br/>:9000]
        API[FastAPI REST<br/>:8000]
        AN[anomaly rules]
        W[silence watcher<br/>every 5s]
    end

    DB[(PostgreSQL)]
    MCP[MCP server<br/>FastMCP - planned]
    DASH[Streamlit dashboard - planned]
    LLM[Claude]

    A1 & A2 & A3 -- "TCP, newline-delimited JSON" --> I
    I --> DB
    I --> AN --> DB
    W --> AN
    API --> DB
    MCP -- HTTP --> API
    DASH -- HTTP --> API
    LLM -- MCP --> MCP
```

## Data flow

1. Each agent samples `/proc` every 2 seconds and sends one JSON object per line over TCP.
2. The ingest server validates each line, upserts the host, stores the metric and runs the
   CPU and memory anomaly rules, all in one transaction.
3. A background task checks every 5 seconds for hosts that went silent.
4. The REST API serves hosts, metrics and anomalies.
5. The dashboard and the MCP server are read-only clients of the REST API. *(planned)*

## Message format

One JSON object per line (`\n`-terminated):

```json
{"host":"agent-1","ts":1760000000,"cpu_percent":37.5,"mem_used_mb":2048,"mem_total_mb":8192,"net_rx_bytes":123456,"net_tx_bytes":65432}
```

| Field | Meaning |
|---|---|
| `host` | Agent name (`AGENT_NAME`) |
| `ts` | Unix time in seconds when the sample was taken |
| `cpu_percent` | CPU busy share since the previous sample, 0-100 |
| `mem_used_mb` / `mem_total_mb` | `MemTotal - MemAvailable` / `MemTotal` from `/proc/meminfo` |
| `net_rx_bytes` / `net_tx_bytes` | Cumulative byte counters, summed over all interfaces except `lo` |

Network counters are cumulative; rates are computed by the readers from consecutive samples.

## Agent (C++17)

```mermaid
flowchart LR
    PROC[/proc/stat<br/>/proc/meminfo<br/>/proc/net/dev/]
    C[Collector thread<br/>every 2s]
    Q[[ThreadSafeQueue&lt;Sample&gt;<br/>bounded, 150]]
    S[Sender thread<br/>TCP + reconnect]
    SRV[Server :9000]
    M[main thread<br/>sigwait SIGINT/SIGTERM]
    STOP{{StopFlag}}

    PROC --> C -- push --> Q -- pop --> S -- JSON line --> SRV
    M -- request_stop --> STOP
    STOP -. wakes .-> C
    STOP -. wakes .-> S
```

### Files

| File | Responsibility |
|---|---|
| `src/collector.{h,cpp}` | Pure parsers for `/proc` (take `std::string`, return `std::optional`), `cpu_percent`, `read_file`, collector thread |
| `src/sender.{h,cpp}` | `to_json`, RAII `Socket`, `connect_to`, `send_all`, `next_backoff`, sender thread |
| `src/queue.h` | `ThreadSafeQueue<T>`: bounded, `std::mutex` + `std::condition_variable`, `close()` for shutdown |
| `src/stop_flag.h` | `StopFlag`: a stop request that sleeping threads can wait on |
| `src/main.cpp` | Configuration from env vars, signal handling, thread startup and shutdown order |

### Threads

- **Collector**: reads a CPU baseline, then every 2 seconds reads `/proc`, builds a `Sample`
  and pushes it to the queue. It waits with `StopFlag::wait_for`, so shutdown is immediate.
- **Sender**: pops samples and sends them as JSON lines. On failure it closes the socket,
  reconnects with exponential backoff (1s doubling up to 30s) and resends the same sample.
- **Main**: blocks `SIGINT`/`SIGTERM` before starting the threads (they inherit the mask),
  then waits for a signal with `sigwait`.

### Shutdown sequence

1. `main` receives `SIGINT`/`SIGTERM` and calls `stop.request_stop()`.
2. The collector wakes from its wait and returns; `main` joins it.
3. `main` calls `queue.close()`. The sender sends whatever is left in the queue
   (if connected) and returns when `pop()` returns `std::nullopt`.
4. `main` joins the sender and exits with code 0.

The producer is stopped before the queue is closed, so no collected sample is rejected.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_NAME` | `agent` | Value of the `host` field |
| `SERVER_HOST` | `127.0.0.1` | Server name or IP (resolved with `getaddrinfo`) |
| `SERVER_PORT` | `9000` | Server TCP port (1-65535; invalid values exit with an error) |

### Build and tests

```bash
cd agent
cmake -S . -B build && cmake --build build
ctest --test-dir build --output-on-failure
```

Dependencies are fetched with CMake `FetchContent` and pinned to releases:
nlohmann/json v3.12.0 and GoogleTest v1.17.0 (tests only; `-DBUILD_TESTING=OFF` skips them).

The test suite covers the parsers (including malformed input), `cpu_percent` edge cases,
the queue and stop flag under concurrency, JSON serialization, and the sender against a
real local TCP server (send, reconnect, prompt shutdown while the server is down).

## Server (Python)

One process runs everything: the FastAPI `lifespan` starts the TCP ingest server and the
silence watcher next to the REST API, all on one asyncio event loop.

```mermaid
flowchart LR
    AG[agents] -- "JSON lines :9000" --> H[handle_client<br/>one coroutine per agent]
    H -- parse_line --> V{valid?}
    V -- no --> LOG[log warning, skip line]
    V -- yes --> T[store_message<br/>in a worker thread]
    T -- "one transaction:<br/>upsert host, insert metric,<br/>CPU/memory rules" --> DB[(PostgreSQL)]
    WT[silence watcher<br/>every 5s] -- host_silent --> DB
    CL[clients] -- "HTTP :8000" --> API[REST endpoints<br/>thread pool] --> DB
```

### Files

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, `lifespan` (startup/shutdown), REST endpoints, response models, silence watcher |
| `app/ingest.py` | `MetricMessage` validation, `parse_line`, `store_message`, TCP server start/stop |
| `app/anomalies.py` | Pure anomaly rules: `check_cpu`, `check_memory`, `check_silence` |
| `app/models.py` | SQLModel tables: `Host`, `Metric`, `Anomaly` |
| `app/db.py` | Engine from `DATABASE_URL`, `create_db_and_tables`, `get_session` |

### Database schema

```mermaid
erDiagram
    hosts ||--o{ metrics : has
    hosts ||--o{ anomalies : has
    hosts {
        int id PK
        varchar name UK
        timestamptz first_seen
        timestamptz last_seen
    }
    metrics {
        int id PK
        int host_id FK
        timestamptz ts
        float cpu_percent
        int mem_used_mb
        int mem_total_mb
        bigint net_rx_bytes
        bigint net_tx_bytes
    }
    anomalies {
        int id PK
        int host_id FK
        timestamptz ts
        varchar type
        float value
        varchar message
    }
```

`metrics` has a composite index on `(host_id, ts)` for the "samples of one host in a time
window" query. Tables are created with `create_all` at startup (no migrations yet).

### Ingest

- One coroutine per connection reads lines with `readline()` (max 64 KB per line).
- Bad lines are logged and skipped; a too-long line or a reset closes only that connection;
  a database error drops only that sample. The server itself never stops on bad input.
- Database writes run in a worker thread (`asyncio.to_thread`), so the event loop keeps
  serving other agents and the REST API. Samples from one agent stay in order.
- On shutdown, the server stops accepting connections and closes the open ones.

### Anomaly rules

| Type | Rule | Where it runs |
|---|---|---|
| `cpu_high` | CPU > 90% for 3 consecutive samples | after each sample, in `store_message` |
| `memory_high` | `mem_used_mb / mem_total_mb` > 90% | after each sample, in `store_message` |
| `host_silent` | no data for more than 30 seconds | background task, every 5 seconds |

Rules are edge-triggered: one anomaly when a condition starts, not one per sample while it lasts.

### REST API

| Endpoint | Returns |
|---|---|
| `GET /health` | `200` if the database answers, `503` if not |
| `GET /hosts` | All hosts with `first_seen` / `last_seen`, sorted by name |
| `GET /hosts/{name}/metrics?minutes=10` | Samples of one host, oldest first; `404` for an unknown host |
| `GET /anomalies?minutes=60` | Anomalies across all hosts with the host name, newest first |

`minutes` must be 1-1440 (otherwise `422`). Interactive docs: `http://localhost:8000/docs`.

### Run and tests

```bash
docker compose up -d --wait db
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000
pytest -v
```

Tests run against a separate database, `syspulse_test`, which the test setup creates and
empties before each test. They cover the anomaly rules (pure), line parsing, storing samples
and anomalies, the silence watcher with a controlled clock, and every REST endpoint.
