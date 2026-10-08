# MCP Demo: "Which host is overloaded and why?"

Claude Code, connected to the SysPulse MCP server (`.mcp.json`), answers an open question
about the monitored hosts by choosing and combining the tools on its own.

## Setup

```bash
docker compose up -d --build --wait          # db, server, agent-1..3
STRESS_SECONDS=120 ./scripts/load_test.sh    # busy loops in agent-1 for 2 minutes
```

Then, in a new Claude Code session (MCP servers load at session start; `/mcp` shows
`syspulse` as connected after approval):

> Which host is overloaded and why?

Run on 2026-10-08. The session language was Hebrew; the answer below is translated.

## Tool calls (chosen by the model)

| # | Call | What it gave the model |
|---|---|---|
| 1 | `compare_hosts(metric="cpu_percent", minutes=10)` | agent-1: max 98.5%, latest 92.6%, avg 4.6%; agent-2 and agent-3 about 0% |
| 2 | `find_anomalies(minutes=60)` | `cpu_high` on agent-1 at 05:59:36 ("CPU above 90% for 3 consecutive samples (now 96.7%)") |
| 3 | `get_host_metrics(host="agent-1", minutes=2)` | The samples: the CPU jump, plus stable memory and quiet network |

The order matches the docstrings: rank first, then anomalies, then raw samples to explain *why*.

## Answer (translated)

> **agent-1 is overloaded, on CPU. Memory and network are fine.**
>
> - `compare_hosts`: only agent-1 stands out: max 98.5%, latest 92.6%. agent-2 and agent-3
>   are about 0%. Its 10-minute average is only 4.6% because the load started less than a
>   minute ago.
> - `find_anomalies`: `cpu_high` on agent-1 at 05:59:36: CPU above 90% for three consecutive
>   samples (96.7%).
> - `get_host_metrics`: until 05:59:22 the CPU was about 0%; at 05:59:24 it jumped to 34.8%,
>   then 88% and 96.5%, and has stayed mostly between 89% and 98.5% since (one short dip to
>   59.9% at 05:59:48). Memory is stable at about 3.4-3.5 GB of 7.8 GB (~44%), far from the 90%
>   threshold. The network is quiet: about 66 bytes in and 215 bytes out per sample, mostly the
>   agent's own traffic.
>
> **Cause:** a sharp, sustained CPU rise in agent-1's container only, with no change in memory
> or network: a process taking all the CPU, here the busy loops of `scripts/load_test.sh`.
> Because the agents measure CPU per container (`CPU_SOURCE=cgroup`), the other hosts are not
> affected.
>
> Note: there is also a `host_silent` anomaly on agent-1 at 05:51 ("No data for 19173 s",
> about 5.3 hours), probably from when the stack was off, not part of the current problem.

## What the demo shows

- **Tool selection from descriptions:** the model was never told which tools to call or in
  which order; it followed the docstrings.
- **Reading the data, not only the ranking:** it noticed that the 10-minute average (4.6%)
  hides a load that started a minute ago, and used max/latest instead.
- **Ruling out other causes:** it checked memory and network before concluding "CPU only".
- **Per-container CPU matters:** with `/proc/stat`, all three agents would have looked loaded
  and the question would have had no single answer.

## What the demo revealed (checked against the API afterwards)

1. **The model's note on `host_silent` was a guess, and it was wrong.** The stack was not off:
   the containers had been running for 10 hours. The laptop slept, so the WSL VM was
   suspended: agents *and* server were frozen (sample gap 20:04 -> 05:51 on all agents). On
   wake-up the silence watcher compared `last_seen` with the current time and reported
   5 hours of silence. Only for agent-1: agent-2 and agent-3 sent a sample a few
   milliseconds before the watcher ran (a race).
   **Fixed on Day 7:** the watcher detects that it was paused itself (more than 15 s since
   its previous round, which should be 5 s) and skips one round. Checked by freezing the
   server container for 60 s: 3 false `host_silent` anomalies before the fix, 0 after.
2. **`cpu_high` fired 4 times during one 2-minute load** (05:59:36, 05:59:54, 06:00:12,
   06:00:40). The rule is edge-triggered without hysteresis: every dip below 90% (like the
   59.9% sample) ends the event, and the next 3 high samples start a new one (flapping).
   The model reported only the first anomaly.
   **Fixed on Day 7** with hysteresis: the event ends only after 3 samples below 80%. The
   same kind of load with a dip now gives one anomaly (see [decisions.md](decisions.md)).
3. **`compare_hosts` ranks by average**, which dilutes a fresh spike. The model handled it,
   but ranking by a recent window or by `latest` would fit "overloaded *now*" better.

Lesson: an LLM's explanation is a hypothesis. The tools give it facts; its interpretation of
them still needs checking.
