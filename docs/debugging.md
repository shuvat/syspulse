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
- Reproduced without sleeping the laptop: `docker compose pause server` freezes the server's
  processes (cgroup freezer) like a sleep does, while the agents keep running:

  ```bash
  docker compose pause server; sleep 60; docker compose unpause server
  ```

  Before the fix: 3 `host_silent` ("No data for 61 s"), one per agent, although every agent
  was alive and its samples were waiting in the socket buffers.
- Fix (Day 7): the watcher skips one round when more than 15 s passed since its previous
  round. After: 0 anomalies, and the log says
  `silence watcher was paused for 64 s, skipping one round`. Checked the other direction
  too: `docker compose stop agent-3` for 45 s still gives `host_silent` for agent-3 only.

## Anomalies: `cpu_high` fires several times during one load

- Symptom: one 2-minute load gave 4 `cpu_high` anomalies (05:59:36, 05:59:54, 06:00:12,
  06:00:40), found with `curl -s "localhost:8000/anomalies?minutes=600"`.
- Cause: the rule is edge-triggered without hysteresis. Samples under load fluctuate around
  90-98% with occasional dips (59.9% once); every dip below 90% ends the event, and the next
  3 high samples start a new one (flapping).
- Fix (Day 7): hysteresis, the event ends only after 3 samples below 80%. Covered by
  `test_flapping_cpu_creates_one_anomaly` (the demo's pattern) and checked for real: two
  20-second loads with a dip of two samples (51%, 0%) in between gave one `cpu_high`.
  Disabling the hysteresis made 7 tests fail.

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

## Dashboard: chart lines disappear although the data is there

- Symptom (in a screenshot of the dashboard during a load test): every line, on both charts,
  vanished for about a minute (11:30:50 -> 11:31:50), and later stopped early.
- First check, the data: no host had a gap longer than 5 seconds, the containers had been up
  the whole time, no `host_silent`. So the problem was in the chart, not in the system.
- Diagnosis: the sample timestamps per host around the "gap":

  ```
  before:  agent-1 :21 :23 :25   agent-2 :21 :23 :25   agent-3 :21 :23 :25
  inside:  agent-1 :56 :58 :00   agent-2 :55 :57 :59   agent-3 :55 :57 :59
  after:   agent-1 :55 :57 :59   agent-2 :55 :57 :59   agent-3 :55 :57 :59
  ```

  Under load, agent-1 moved by one second. The agent runs in the same container as the load,
  limited to one CPU, so its collector thread woke up a little late every interval (CFS
  throttling also slows the monitoring process) until the delay added up to a second.
- Cause: the chart used a wide table (one row per timestamp, one column per host). With
  misaligned timestamps, every host had a value only in every other row, NaN in between.
  The chart breaks a line at NaN, and a line of isolated points is not drawn. In the
  current 15 minutes, 487 of 1836 cells of that table were NaN.
- Fix: long format (one row per sample, `color="host"`), so each host's line uses only its
  own samples; plus `break_gaps`, which breaks a line where one host has no data for more
  than 10 seconds (before, a straight line was drawn across the time the stack was stopped).
- Checked without a browser: the chart spec that Streamlit builds was rendered to PNG with
  `vl-convert` (in a throwaway venv) on the same real data: before, the gap was reproduced;
  after, continuous lines. A synthetic 60-second outage showed the line breaking.

## GDB

Build: `agent/build` is Debug (`-g`, no optimization), so GDB shows source lines and
variables. The sessions below were recorded with `gdb -batch -x <commands file>`; the same
commands work interactively (see "Try it yourself").

### Session 1: the healthy agent

```gdb
break collector.cpp:275        # right before a sample enters the queue
run
info threads
print sample
```

```
Thread 2 "syspulse_agent" hit Breakpoint 1, syspulse::run_collector (host="agent-gdb", ...,
    interval=std::chrono::duration = { 2000ms }, cpu_source=syspulse::CpuSource::Cgroup) at collector.cpp:275
275	        queue.push(std::move(sample));

  Id   Target Id                                   Frame
  1    Thread ... (LWP 60246) "syspulse_agent" __GI___sigtimedwait (...)          <- main: sigwait()
* 2    Thread ... (LWP 60249) "syspulse_agent" syspulse::run_collector (...)      <- collector (stopped here)
  3    Thread ... (LWP 60250) "syspulse_agent" __futex_abstimed_wait_common64 ()  <- sender: waiting in pop()

$1 = {
  host = "agent-gdb", ts = 1791446847, cpu_percent = 13.983709435439401,
  mem_used_mb = 3805, mem_total_mb = 7792, net_rx_bytes = 274205444, net_tx_bytes = 37245471
}
```

The three threads match the design: main waits for a signal, the sender waits on the queue's
condition variable, the collector works. A breakpoint stops only the thread that hits it
from GDB's point of view (`*` marks the current thread), but in all-stop mode every thread
is paused.

Conditional breakpoint: stop only when the cgroup used more than 1 second of CPU in one
interval (a busy loop was running in the background):

```gdb
break cgroup_cpu_percent if cur_usage_usec - prev_usage_usec > 1000000
continue
info args
finish
```

```
prev_usage_usec = 2369145494
cur_usage_usec = 2371619572
elapsed_usec = 2176599
cpu_capacity = 8
Run till exit ...
Value returned is $3 = 14.208393461542526
```

2.47 s of CPU in 2.18 s on 8 CPUs = 14.2%. `finish` runs to the end of the function and
prints its return value: the formula can be checked by hand against real inputs.

### Session 2: a planted deadlock

Planted bug in `ThreadSafeQueue::push()`, inside the locked block:

```cpp
std::lock_guard<std::mutex> lock(mutex_);
...
if (size() >= capacity_) {  // PLANTED BUG: size() locks mutex_ again
```

It compiles without a warning. Symptom, without a debugger: `nc` receives 0 lines, the agent
log shows no error, the process is in state `Sl` at 0.1% CPU (waiting, not spinning), and it
**ignores SIGTERM**: main calls `collector.join()`, which never returns. In Docker that means
`docker stop` waits 10 seconds and then kills it (exit 137).

Attaching to the running process failed:

```
Could not attach to process.  If your uid matches the uid of the target
process, check the setting of /proc/sys/kernel/yama/ptrace_scope, or try
again as the root user.
```

`ptrace_scope=1` (Ubuntu default, Yama LSM): a process may only be traced by its parent (or
root). Otherwise any program of the same user could read another one's memory (passwords,
tokens). So the agent was started under GDB, and stopped after 5 seconds with a signal
(`handle SIGUSR1 stop print nopass`; interactively, Ctrl+C does the same).

```gdb
info threads
thread apply all bt 8
```

```
Thread 3 (LWP 60531):                                    <- sender
#3  __pthread_cond_wait_common (mutex=0x7fffffffd3c0, cond=0x7fffffffd3e8)
#6  syspulse::ThreadSafeQueue<syspulse::Sample>::pop (this=0x7fffffffd3c0) at queue.h:49
#7  syspulse::run_sender (...) at sender.cpp:98

Thread 2 (LWP 60530):                                    <- collector
#0  futex_wait (private=0, expected=2, futex_word=0x7fffffffd3c0)
#1  __GI___lll_lock_wait (futex=0x7fffffffd3c0, private=0)
#5  std::mutex::lock (this=0x7fffffffd3c0)
#7  syspulse::ThreadSafeQueue<syspulse::Sample>::size (this=0x7fffffffd3c0) at queue.h:68

Thread 1 (LWP 60527):                                    <- main
#2  main () at main.cpp:96                               (sigwait)
```

The collector is blocked locking the queue's mutex (`0x7fffffffd3c0`, the same address as the
queue: the mutex is its first member). The sender waits for an item that never comes.
Who holds the mutex?

```gdb
thread 2
frame 8
print mutex_._M_mutex.__data.__owner
```

```
#8  syspulse::ThreadSafeQueue<syspulse::Sample>::push (...) at queue.h:31
31	            if (size() >= capacity_) {  // PLANTED BUG: size() locks mutex_ again
$1 = 60622
* 2    Thread 0x7ffff77ff6c0 (LWP 60622) "syspulse_agent" futex_wait (...)
```

The owner (LWP 60622) is the waiting thread itself: a **self-deadlock**. `push()` holds the
lock and calls `size()`, which locks the same non-recursive `std::mutex` again.
(`_M_mutex` is the `pthread_mutex_t` inside `std::mutex`; glibc stores the owner's thread id.)
Fix: use `items_.size()` directly inside the locked block. Then the bug was removed (no diff).

Fixing it with `std::recursive_mutex` would hide the design problem: the usual rule is that
public methods lock and are never called while the lock is held.

### Try it yourself (interactive)

```bash
cd ~/syspulse/agent
nc -lk 9000 > /dev/null &          # receiver
gdb ./build/syspulse_agent
```

```gdb
(gdb) break collector.cpp:275
(gdb) run
(gdb) info threads
(gdb) bt
(gdb) print sample
(gdb) next                  # one line, without entering functions
(gdb) continue
(gdb) delete                # remove all breakpoints
(gdb) continue
^C                          # Ctrl+C stops every thread
(gdb) thread apply all bt
(gdb) quit
```

`kill %1` stops `nc` afterwards.

### Gotchas

- `interrupt` after `run &` does not stop the program in `gdb -batch` ("Selected thread is
  running"); a signal from outside does (`handle SIGUSR1 stop print nopass`). SIGINT cannot be
  used for that here: the agent blocks it (for `sigwait`), so it is never delivered.
- `cd dir && nc ... & NC=$!`: `$!` is the PID of the subshell running the whole `&&` chain,
  not of `nc`; `kill $NC` left `nc` running. Run background commands on their own line.

## Valgrind (memory leaks and invalid memory access)

Valgrind's memcheck runs the program on a synthetic CPU and tracks every byte: allocated or
not, initialized or not, freed or not. It needs no special build (unlike ASan or TSan), but
a Debug build (`-g`) makes reports show file and line. Programs run about 20-50x slower.

### The real agent

Run for about 25 seconds against `nc` (the server's port 9000 is not published outside
Compose), with the receiver restarted in the middle to cover the reconnect path, and Ctrl+C
(SIGINT) at the end to cover the shutdown path:

```bash
# Terminal 1
nc -lk 9000
# Terminal 2 (from agent/)
AGENT_NAME=agent-vg CPU_SOURCE=cgroup valgrind --leak-check=full --show-leak-kinds=all \
    --track-origins=yes --error-exitcode=1 ./build/syspulse_agent
# After ~10s: Ctrl+C nc, wait 5s, start it again; after ~10s more: Ctrl+C the agent.
```

Agent log: `connected`, `send failed, reconnecting`, `retrying in 1000 ms`, `retrying in
2000 ms`, `connected`, `received SIGINT, shutting down`, `dropped samples: 0`. Valgrind:

```
HEAP SUMMARY:
    in use at exit: 0 bytes in 0 blocks
  total heap usage: 1,420 allocs, 1,420 frees, 624,258 bytes allocated
All heap blocks were freed -- no leaks are possible
ERROR SUMMARY: 0 errors from 0 contexts (suppressed: 0 from 0)
```

Every allocation was freed, including the threads, the queue and the socket, so the graceful
shutdown cleans up completely. No invalid reads/writes and no use of uninitialized values.

### The unit tests

```bash
valgrind --leak-check=full --show-leak-kinds=all --track-origins=yes --error-exitcode=1 \
    ./build/agent_tests
```

They cover paths a normal run does not reach (malformed parser input, connection failures,
the queue stress test). Result: 52 tests passed, 1,959 allocs / 1,959 frees, 0 errors, in
about 4 seconds. This also runs in CI (the `cpp` job), so a future leak fails the build.

### Reading a leak report: planted bug

To check that valgrind really catches leaks, a 1 KB leak per sample was planted temporarily
in the collector loop (`new char[1024]`, never deleted) and the collector test was run:

```
2,048 bytes in 2 blocks are definitely lost in loss record 1 of 1
   at 0x48485C3: operator new[](unsigned long) (in .../vgpreload_memcheck-amd64-linux.so)
   by 0x17069A: syspulse::run_collector(...) (collector.cpp:276)
   by ...: std::thread::_Invoker<...>  ...
   by ...: start_thread (pthread_create.c:447)
```

Exit code 1. The stack is read top-down: who allocated (`operator new[]`), from where
(`collector.cpp:276`, our line), and in which thread (started by `std::thread`). 2 blocks =
2 samples during the test. Then the line was removed (no diff left).

Leak kinds in the summary:

| Kind | Meaning |
|---|---|
| definitely lost | No pointer to the block remains: a real leak |
| indirectly lost | Reachable only from a lost block (e.g. nodes of a lost list) |
| possibly lost | Only a pointer into the middle of the block remains: suspicious |
| still reachable | Still pointed to at exit, never freed: usually not a bug (e.g. global caches) |

### Gotchas while scripting this

- `cmd1 && cmd2 && ... && nc ... &`: the `&` sends the whole `&&` chain to the background,
  so a variable set in that chain does not exist in the current shell. Paths built from it
  became `/nc1.out` ("Permission denied").
- The failed attempt left an `nc` running on port 9000. The next run's `nc` also bound to
  the port (OpenBSD `nc` uses `SO_REUSEPORT`), and the agent stayed connected to the old one,
  so killing "our" `nc` tested no reconnect. Found because the agent log had no
  `send failed`. Check before a test: `pgrep -a nc`, `ss -ltn 'sport = :9000'`.
