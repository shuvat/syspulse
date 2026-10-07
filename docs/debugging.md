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

## GDB

*(Day 6: breakpoints, `info threads`, `thread apply all bt`, planted bug.)*

## Valgrind

*(Day 6: `valgrind --leak-check=full` on the agent.)*
