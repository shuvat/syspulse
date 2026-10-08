#pragma once

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>

#include "queue.h"
#include "stop_flag.h"

namespace syspulse {

// One metrics sample, matching the wire format sent to the server.
struct Sample {
    std::string host;
    int64_t ts = 0;  // Unix time in seconds.
    double cpu_percent = 0.0;
    uint64_t mem_used_mb = 0;
    uint64_t mem_total_mb = 0;
    uint64_t net_rx_bytes = 0;
    uint64_t net_tx_bytes = 0;
};

// Cumulative CPU time counters from the first "cpu " line of /proc/stat,
// measured in clock ticks (USER_HZ) since boot.
struct CpuTimes {
    uint64_t user = 0;
    uint64_t nice = 0;
    uint64_t system = 0;
    uint64_t idle = 0;
    uint64_t iowait = 0;
    uint64_t irq = 0;
    uint64_t softirq = 0;
    uint64_t steal = 0;

    // Time the CPU was not doing work (waiting for I/O counts as idle).
    uint64_t idle_total() const { return idle + iowait; }
    uint64_t total() const {
        return user + nice + system + idle + iowait + irq + softirq + steal;
    }
};

struct MemInfo {
    uint64_t total_mb = 0;
    uint64_t used_mb = 0;
};

// Cumulative byte counters summed over all interfaces except loopback.
struct NetCounters {
    uint64_t rx_bytes = 0;
    uint64_t tx_bytes = 0;
};

// All parsers take the file content (not a path) so they can be unit-tested
// with fixed strings. They return std::nullopt on malformed input.
std::optional<CpuTimes> parse_cpu_times(const std::string& proc_stat);
std::optional<MemInfo> parse_meminfo(const std::string& proc_meminfo);
std::optional<NetCounters> parse_net_dev(const std::string& proc_net_dev);

// CPU usage in percent [0, 100] between two samples of /proc/stat.
double cpu_percent(const CpuTimes& prev, const CpuTimes& cur);

// Total CPU time (microseconds) used by a cgroup, from the "usage_usec" line of
// a cgroup v2 cpu.stat file. Inside a container this covers only that container,
// while /proc/stat always describes the whole machine.
std::optional<uint64_t> parse_cgroup_cpu_usage(const std::string& cpu_stat);

// CPU limit of a cgroup in CPUs (e.g. 1.5), from a cgroup v2 cpu.max file
// ("<quota_usec> <period_usec>", e.g. "150000 100000"). Returns std::nullopt for
// "max" (no limit) and for malformed input: both mean "no usable limit".
std::optional<double> parse_cgroup_cpu_limit(const std::string& cpu_max);

// How many CPUs a cgroup can use: its limit, but never more than the machine has.
// Without a limit (std::nullopt), all `online_cpus`.
double effective_cpu_capacity(std::optional<double> limit_cpus, unsigned online_cpus);

// CPU usage in percent [0, 100] of `cpu_capacity` CPUs between two cpu.stat
// readings taken `elapsed_usec` apart. 100 means the cgroup used its whole
// capacity (its CPU limit, or every CPU of the machine if it has none).
double cgroup_cpu_percent(uint64_t prev_usage_usec, uint64_t cur_usage_usec,
                          uint64_t elapsed_usec, double cpu_capacity);

// Reads a whole file into a string. The only function here that touches disk.
std::optional<std::string> read_file(const std::string& path);

// Where CPU usage is measured. Proc: the whole machine (/proc/stat).
// Cgroup: only this process's cgroup, i.e. its container (cgroup v2 cpu.stat).
enum class CpuSource { Proc, Cgroup };

// Collector thread body: every `interval`, reads /proc (and cpu.stat for
// CpuSource::Cgroup) and pushes a Sample. Returns when `stop` is requested.
void run_collector(const std::string& host, ThreadSafeQueue<Sample>& queue, StopFlag& stop,
                   std::chrono::milliseconds interval, CpuSource cpu_source);

}  // namespace syspulse
