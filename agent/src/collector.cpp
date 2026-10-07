#include "collector.h"

#include <algorithm>
#include <fstream>
#include <sstream>

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

}  // namespace syspulse
