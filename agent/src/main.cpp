// Temporary main for Day 1 step 2: reads /proc once per second for two
// samples and prints the parsed values. Will be replaced by the
// collector/sender threads in a later step.
#include <chrono>
#include <iostream>
#include <thread>

#include "collector.h"

using namespace syspulse;

static std::optional<CpuTimes> read_cpu() {
    auto text = read_file("/proc/stat");
    return text ? parse_cpu_times(*text) : std::nullopt;
}

int main() {
    auto cpu_prev = read_cpu();
    std::this_thread::sleep_for(std::chrono::seconds(1));
    auto cpu_cur = read_cpu();

    auto mem_text = read_file("/proc/meminfo");
    auto net_text = read_file("/proc/net/dev");
    auto mem = mem_text ? parse_meminfo(*mem_text) : std::nullopt;
    auto net = net_text ? parse_net_dev(*net_text) : std::nullopt;

    if (!cpu_prev || !cpu_cur || !mem || !net) {
        std::cerr << "failed to read or parse /proc\n";
        return 1;
    }

    std::cout << "cpu_percent:  " << cpu_percent(*cpu_prev, *cpu_cur) << "\n"
              << "mem_used_mb:  " << mem->used_mb << "\n"
              << "mem_total_mb: " << mem->total_mb << "\n"
              << "net_rx_bytes: " << net->rx_bytes << "\n"
              << "net_tx_bytes: " << net->tx_bytes << "\n";
    return 0;
}
