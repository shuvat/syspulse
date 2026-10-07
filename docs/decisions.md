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
  counters are cumulative since boot. Idle time is `idle + iowait`.
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

## Tests against a real PostgreSQL database
- The code uses PostgreSQL-specific features (`ON CONFLICT`, `timestamptz`), so SQLite would
  test different behavior than production.
- Tests use a separate `syspulse_test` database, emptied before each test. The cost is a
  dependency on a running PostgreSQL; CI will provide it as a service container.

## AI integration: MCP server
- Why: (fill in on Day 4)
