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
- Why: (fill in on Day 2)

## AI integration: MCP server
- Why: (fill in on Day 4)
