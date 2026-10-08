#include <gtest/gtest.h>
#include <chrono>
#include <functional>
#include <thread>
#include "collector.h"

using namespace syspulse;
using namespace std::chrono_literals;

namespace {

// Runs the collector until it produces one sample, then stops it.
Sample collect_one(CpuSource cpu_source, MemSource mem_source) {
    ThreadSafeQueue<Sample> queue(100);
    StopFlag stop;

    std::thread collector(run_collector, "test-host", std::ref(queue), std::ref(stop), 20ms,
                          cpu_source, mem_source);
    auto sample = queue.pop();  // Blocks until the first sample arrives.
    stop.request_stop();
    collector.join();  // Returns quickly because wait_for() is woken by stop.

    EXPECT_TRUE(sample.has_value());
    return sample.value_or(Sample{});
}

void expect_valid(const Sample& sample) {
    EXPECT_EQ(sample.host, "test-host");
    EXPECT_GT(sample.ts, 0);
    EXPECT_GE(sample.cpu_percent, 0.0);
    EXPECT_LE(sample.cpu_percent, 100.0);
    EXPECT_GT(sample.mem_total_mb, 0u);
    EXPECT_LE(sample.mem_used_mb, sample.mem_total_mb);
}

}  // namespace

// These read the real /proc and /sys/fs/cgroup, so they only run on Linux
// with cgroup v2 (our target and the CI runners).
TEST(RunCollector, PushesSamplesFromProc) {
    expect_valid(collect_one(CpuSource::Proc, MemSource::Proc));
}

TEST(RunCollector, PushesSamplesFromCgroup) {
    expect_valid(collect_one(CpuSource::Cgroup, MemSource::Cgroup));
}
