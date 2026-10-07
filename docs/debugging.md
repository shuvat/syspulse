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

## GDB

*(Day 6: breakpoints, `info threads`, `thread apply all bt`, planted bug.)*

## Valgrind

*(Day 6: `valgrind --leak-check=full` on the agent.)*
