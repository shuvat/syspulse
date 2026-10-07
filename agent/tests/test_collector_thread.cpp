#include <gtest/gtest.h>
#include <chrono>
#include <functional>
#include <thread>
#include "collector.h"

using namespace syspulse;
using namespace std::chrono_literals;

// Reads the real /proc, so it only runs on Linux (which is our target and CI).
TEST(RunCollector, PushesSamplesUntilStopped) {
    ThreadSafeQueue<Sample> queue(100);
    StopFlag stop;

    std::thread collector(run_collector, "test-host", std::ref(queue), std::ref(stop), 20ms);
    auto sample = queue.pop();  // Blocks until the first sample arrives.
    stop.request_stop();
    collector.join();  // Returns quickly because wait_for() is woken by stop.

    ASSERT_TRUE(sample.has_value());
    EXPECT_EQ(sample->host, "test-host");
    EXPECT_GT(sample->ts, 0);
    EXPECT_GE(sample->cpu_percent, 0.0);
    EXPECT_LE(sample->cpu_percent, 100.0);
    EXPECT_GT(sample->mem_total_mb, 0u);
    EXPECT_LE(sample->mem_used_mb, sample->mem_total_mb);
}
