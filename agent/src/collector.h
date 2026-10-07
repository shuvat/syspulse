#pragma once

#include <cstdint>
#include <optional>
#include <string>

namespace syspulse {

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

// Reads a whole file into a string. The only function here that touches disk.
std::optional<std::string> read_file(const std::string& path);

}  // namespace syspulse
