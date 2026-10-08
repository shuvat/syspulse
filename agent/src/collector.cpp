#include "collector.h"

#include <algorithm>
#include <charconv>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>

namespace syspulse {

std::optional<CpuTimes> parse_cpu_times(const std::string& proc_stat) {
    std::istringstream input(proc_stat);
    std::string line;
    while (std::getline(input, line)) {
        // "cpu " (with a space) is the aggregate line; "cpu0", "cpu1"... are per-core.
        if (line.rfind("cpu ", 0) != 0) {
            continue;
        }
        std::istringstream fields(line);
        std::string label;
        CpuTimes t;
        fields >> label >> t.user >> t.nice >> t.system >> t.idle >> t.iowait >> t.irq >>
            t.softirq >> t.steal;
        if (fields.fail()) {
            return std::nullopt;
        }
        return t;
    }
    return std::nullopt;
}

double cpu_percent(const CpuTimes& prev, const CpuTimes& cur) {
    // Counters only grow; if they did not, there is no interval to measure.
    if (cur.total() <= prev.total()) {
        return 0.0;
    }
    // Use double: iowait is known to occasionally go backwards on Linux,
    // so the idle delta may be negative and unsigned math would wrap around.
    double total_delta = static_cast<double>(cur.total() - prev.total());
    double idle_delta =
        static_cast<double>(cur.idle_total()) - static_cast<double>(prev.idle_total());
    double percent = (total_delta - idle_delta) / total_delta * 100.0;
    return std::clamp(percent, 0.0, 100.0);
}

std::optional<uint64_t> parse_cgroup_cpu_usage(const std::string& cpu_stat) {
    // Format: one "key value" pair per line, e.g. "usage_usec 251643".
    std::istringstream input(cpu_stat);
    std::string line;
    while (std::getline(input, line)) {
        std::istringstream fields(line);
        std::string key;
        uint64_t value = 0;
        if (fields >> key >> value && key == "usage_usec") {
            return value;
        }
    }
    return std::nullopt;
}

std::optional<double> parse_cgroup_cpu_limit(const std::string& cpu_max) {
    // Format: "<quota> <period>" on one line, e.g. "100000 100000" = 1 CPU,
    // or "max 100000" = no limit.
    std::istringstream fields(cpu_max);
    std::string quota_text;
    uint64_t period = 0;
    if (!(fields >> quota_text >> period) || quota_text == "max" || period == 0) {
        return std::nullopt;
    }
    uint64_t quota = 0;
    auto [end, ec] =
        std::from_chars(quota_text.data(), quota_text.data() + quota_text.size(), quota);
    if (ec != std::errc() || end != quota_text.data() + quota_text.size() || quota == 0) {
        return std::nullopt;
    }
    return static_cast<double>(quota) / static_cast<double>(period);
}

double effective_cpu_capacity(std::optional<double> limit_cpus, unsigned online_cpus) {
    // A limit above the number of CPUs (e.g. --cpus=16 on an 8-CPU machine)
    // cannot be reached, so it is not a useful denominator.
    if (limit_cpus && *limit_cpus < online_cpus) {
        return *limit_cpus;
    }
    return online_cpus;
}

double cgroup_cpu_percent(uint64_t prev_usage_usec, uint64_t cur_usage_usec,
                          uint64_t elapsed_usec, double cpu_capacity) {
    // No interval, no capacity, or a counter that went backwards (e.g. the cgroup
    // was recreated): there is nothing meaningful to measure.
    if (elapsed_usec == 0 || cpu_capacity <= 0.0 || cur_usage_usec < prev_usage_usec) {
        return 0.0;
    }
    // The CPU time available in the interval is wall time on every CPU the
    // cgroup may use.
    double used = static_cast<double>(cur_usage_usec - prev_usage_usec);
    double available = static_cast<double>(elapsed_usec) * cpu_capacity;
    // Clamp: the two readings and the wall clock are not taken at exactly the
    // same instant, so the ratio can slightly exceed 100%.
    return std::clamp(used / available * 100.0, 0.0, 100.0);
}

std::optional<MemInfo> parse_meminfo(const std::string& proc_meminfo) {
    std::istringstream input(proc_meminfo);
    std::string line;
    std::optional<uint64_t> total_kb;
    std::optional<uint64_t> available_kb;

    while (std::getline(input, line)) {
        std::istringstream fields(line);
        std::string key;
        uint64_t value = 0;
        if (!(fields >> key >> value)) {
            continue;  // Skip lines we cannot parse; we only need two keys.
        }
        if (key == "MemTotal:") {
            total_kb = value;
        } else if (key == "MemAvailable:") {
            available_kb = value;
        }
    }

    if (!total_kb || !available_kb || *available_kb > *total_kb) {
        return std::nullopt;
    }
    MemInfo info;
    info.total_mb = *total_kb / 1024;
    info.used_mb = (*total_kb - *available_kb) / 1024;
    return info;
}

std::optional<NetCounters> parse_net_dev(const std::string& proc_net_dev) {
    std::istringstream input(proc_net_dev);
    std::string line;

    // The first two lines are column headers.
    for (int i = 0; i < 2; ++i) {
        if (!std::getline(input, line)) {
            return std::nullopt;
        }
    }

    NetCounters counters;
    while (std::getline(input, line)) {
        // Format: "  eth0: rx_bytes rx_packets ... (8 rx fields) tx_bytes ..."
        auto colon = line.find(':');
        if (colon == std::string::npos) {
            return std::nullopt;
        }
        std::istringstream name_stream(line.substr(0, colon));
        std::string name;
        name_stream >> name;  // Trims the leading spaces.
        if (name == "lo") {
            continue;
        }

        std::istringstream fields(line.substr(colon + 1));
        uint64_t values[9];
        for (uint64_t& v : values) {
            fields >> v;
        }
        if (fields.fail()) {
            return std::nullopt;
        }
        counters.rx_bytes += values[0];  // Column 1: received bytes.
        counters.tx_bytes += values[8];  // Column 9: transmitted bytes.
    }
    return counters;
}

std::optional<std::string> read_file(const std::string& path) {
    std::ifstream file(path);
    if (!file) {
        return std::nullopt;
    }
    // /proc files report size 0, so read the stream until EOF instead of
    // asking for the file size first.
    std::ostringstream content;
    content << file.rdbuf();
    return content.str();
}

namespace {

// One cumulative CPU reading. CPU % is computed from the delta of two readings;
// only the fields of the chosen CpuSource are filled in.
struct CpuReading {
    CpuTimes times;                                   // CpuSource::Proc
    uint64_t usage_usec = 0;                          // CpuSource::Cgroup
    std::chrono::steady_clock::time_point taken_at;  // CpuSource::Cgroup
    double cpu_capacity = 0.0;                        // CpuSource::Cgroup
};

std::optional<CpuReading> read_cpu(CpuSource source) {
    CpuReading reading;
    if (source == CpuSource::Proc) {
        auto text = read_file("/proc/stat");
        auto times = text ? parse_cpu_times(*text) : std::nullopt;
        if (!times) {
            return std::nullopt;
        }
        reading.times = *times;
    } else {
        auto text = read_file("/sys/fs/cgroup/cpu.stat");
        auto usage = text ? parse_cgroup_cpu_usage(*text) : std::nullopt;
        if (!usage) {
            return std::nullopt;
        }
        reading.usage_usec = *usage;
        // steady_clock, not system_clock: it never jumps (e.g. NTP adjustments),
        // so the measured interval is always the real elapsed time.
        reading.taken_at = std::chrono::steady_clock::now();
        // Read on every sample, because the limit can change at runtime
        // (docker update --cpus). A missing file (e.g. the root cgroup) means no limit.
        auto max_text = read_file("/sys/fs/cgroup/cpu.max");
        auto limit = max_text ? parse_cgroup_cpu_limit(*max_text) : std::nullopt;
        // hardware_concurrency() may return 0 if unknown; cgroup_cpu_percent() then returns 0.
        reading.cpu_capacity = effective_cpu_capacity(limit, std::thread::hardware_concurrency());
    }
    return reading;
}

double cpu_percent_between(CpuSource source, const CpuReading& prev, const CpuReading& cur) {
    if (source == CpuSource::Proc) {
        return cpu_percent(prev.times, cur.times);
    }
    auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(cur.taken_at - prev.taken_at);
    return cgroup_cpu_percent(prev.usage_usec, cur.usage_usec,
                              static_cast<uint64_t>(elapsed.count()), cur.cpu_capacity);
}

std::optional<MemInfo> read_meminfo() {
    auto text = read_file("/proc/meminfo");
    return text ? parse_meminfo(*text) : std::nullopt;
}

std::optional<NetCounters> read_net_dev() {
    auto text = read_file("/proc/net/dev");
    return text ? parse_net_dev(*text) : std::nullopt;
}

int64_t unix_time_now() {
    using namespace std::chrono;
    return duration_cast<seconds>(system_clock::now().time_since_epoch()).count();
}

}  // namespace

void run_collector(const std::string& host, ThreadSafeQueue<Sample>& queue, StopFlag& stop,
                   std::chrono::milliseconds interval, CpuSource cpu_source) {
    // CPU % needs two readings, so the first one is only a baseline.
    std::optional<CpuReading> prev_cpu = read_cpu(cpu_source);

    while (!stop.wait_for(interval)) {
        auto cpu = read_cpu(cpu_source);
        auto mem = read_meminfo();
        auto net = read_net_dev();

        if (!prev_cpu || !cpu || !mem || !net) {
            std::cerr << "collector: failed to read CPU, memory or network stats, skipping sample\n";
            prev_cpu = cpu;
            continue;
        }

        Sample sample;
        sample.host = host;
        sample.ts = unix_time_now();
        sample.cpu_percent = cpu_percent_between(cpu_source, *prev_cpu, *cpu);
        sample.mem_used_mb = mem->used_mb;
        sample.mem_total_mb = mem->total_mb;
        sample.net_rx_bytes = net->rx_bytes;
        sample.net_tx_bytes = net->tx_bytes;
        queue.push(std::move(sample));

        prev_cpu = cpu;
    }
}

}  // namespace syspulse
