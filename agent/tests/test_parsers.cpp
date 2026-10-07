#include <gtest/gtest.h>

#include "collector.h"

using namespace syspulse;

// ---------- /proc/stat ----------

TEST(ParseCpuTimes, ParsesAggregateLine) {
    // Real format: aggregate "cpu" line, then per-core lines.
    // Modern kernels add guest and guest_nice columns, which we ignore.
    const std::string text =
        "cpu  100 5 50 800 40 3 2 1 0 0\n"
        "cpu0 50 2 25 400 20 1 1 0 0 0\n"
        "intr 12345\n";

    auto t = parse_cpu_times(text);

    ASSERT_TRUE(t.has_value());
    EXPECT_EQ(t->user, 100u);
    EXPECT_EQ(t->nice, 5u);
    EXPECT_EQ(t->system, 50u);
    EXPECT_EQ(t->idle, 800u);
    EXPECT_EQ(t->iowait, 40u);
    EXPECT_EQ(t->irq, 3u);
    EXPECT_EQ(t->softirq, 2u);
    EXPECT_EQ(t->steal, 1u);
}

TEST(ParseCpuTimes, ReturnsNulloptOnEmptyInput) {
    EXPECT_FALSE(parse_cpu_times("").has_value());
}

TEST(ParseCpuTimes, ReturnsNulloptWithoutAggregateLine) {
    // "cpu0" must not be mistaken for the aggregate "cpu " line.
    EXPECT_FALSE(parse_cpu_times("cpu0 1 2 3 4 5 6 7 8\n").has_value());
}

TEST(ParseCpuTimes, ReturnsNulloptOnTooFewFields) {
    EXPECT_FALSE(parse_cpu_times("cpu  1 2 3 4\n").has_value());
}

TEST(ParseCpuTimes, ReturnsNulloptOnNonNumericField) {
    EXPECT_FALSE(parse_cpu_times("cpu  1 2 abc 4 5 6 7 8\n").has_value());
}

// ---------- cpu_percent ----------

TEST(CpuPercent, ComputesBusyShareOfDelta) {
    CpuTimes prev;
    prev.user = 100;
    prev.idle = 300;
    CpuTimes cur;
    cur.user = 200;
    cur.idle = 600;

    // Delta: 400 ticks total, 300 idle -> 100 busy -> 25%.
    EXPECT_DOUBLE_EQ(cpu_percent(prev, cur), 25.0);
}

TEST(CpuPercent, ReturnsZeroWhenNoTimePassed) {
    CpuTimes t;
    t.user = 100;
    t.idle = 300;
    EXPECT_DOUBLE_EQ(cpu_percent(t, t), 0.0);
}

TEST(CpuPercent, StaysInRangeWhenIowaitGoesBackwards) {
    // iowait drops by 40 while user grows: the idle delta is negative.
    // With unsigned math this would wrap to a huge number.
    CpuTimes prev;
    prev.idle = 100;
    prev.iowait = 50;
    CpuTimes cur;
    cur.user = 100;
    cur.idle = 100;
    cur.iowait = 10;

    EXPECT_DOUBLE_EQ(cpu_percent(prev, cur), 100.0);
}

// ---------- cgroup v2 cpu.stat ----------

TEST(ParseCgroupCpuUsage, ReadsUsageUsec) {
    // Real format from /sys/fs/cgroup/cpu.stat inside a container.
    const std::string text =
        "usage_usec 251643\n"
        "user_usec 83881\n"
        "system_usec 167762\n"
        "nr_periods 0\n";

    auto usage = parse_cgroup_cpu_usage(text);

    ASSERT_TRUE(usage.has_value());
    EXPECT_EQ(*usage, 251643u);
}

TEST(ParseCgroupCpuUsage, FindsUsageUsecOnAnyLine) {
    EXPECT_EQ(parse_cgroup_cpu_usage("user_usec 5\nusage_usec 42\n"), 42u);
}

TEST(ParseCgroupCpuUsage, ReturnsNulloptOnEmptyInput) {
    EXPECT_FALSE(parse_cgroup_cpu_usage("").has_value());
}

TEST(ParseCgroupCpuUsage, ReturnsNulloptWithoutUsageUsec) {
    EXPECT_FALSE(parse_cgroup_cpu_usage("user_usec 5\nsystem_usec 7\n").has_value());
}

TEST(ParseCgroupCpuUsage, ReturnsNulloptOnNonNumericValue) {
    EXPECT_FALSE(parse_cgroup_cpu_usage("usage_usec abc\n").has_value());
}

// ---------- cgroup_cpu_percent ----------

TEST(CgroupCpuPercent, ComputesShareOfAllCpus) {
    // 2s on 4 CPUs = 8s of CPU time available; 2s used -> 25%.
    EXPECT_DOUBLE_EQ(cgroup_cpu_percent(1'000'000, 3'000'000, 2'000'000, 4), 25.0);
}

TEST(CgroupCpuPercent, OneBusyCpuOutOfEight) {
    // A single-threaded busy loop on an 8-CPU machine.
    EXPECT_DOUBLE_EQ(cgroup_cpu_percent(0, 2'000'000, 2'000'000, 8), 12.5);
}

TEST(CgroupCpuPercent, ClampsToHundred) {
    // Slightly more usage than wall time (readings are not simultaneous).
    EXPECT_DOUBLE_EQ(cgroup_cpu_percent(0, 2'100'000, 1'000'000, 2), 100.0);
}

TEST(CgroupCpuPercent, ReturnsZeroWhenCounterGoesBackwards) {
    EXPECT_DOUBLE_EQ(cgroup_cpu_percent(5'000'000, 1'000'000, 2'000'000, 4), 0.0);
}

TEST(CgroupCpuPercent, ReturnsZeroWithoutElapsedTimeOrCpus) {
    EXPECT_DOUBLE_EQ(cgroup_cpu_percent(0, 1'000'000, 0, 4), 0.0);
    EXPECT_DOUBLE_EQ(cgroup_cpu_percent(0, 1'000'000, 2'000'000, 0), 0.0);
}

// ---------- /proc/meminfo ----------

TEST(ParseMeminfo, ComputesUsedFromAvailable) {
    const std::string text =
        "MemTotal:        8388608 kB\n"
        "MemFree:         1048576 kB\n"
        "MemAvailable:    6291456 kB\n"
        "Buffers:          204800 kB\n";

    auto m = parse_meminfo(text);

    ASSERT_TRUE(m.has_value());
    EXPECT_EQ(m->total_mb, 8192u);
    EXPECT_EQ(m->used_mb, 2048u);  // 8192 - 6144, not based on MemFree.
}

TEST(ParseMeminfo, ReturnsNulloptWithoutMemAvailable) {
    EXPECT_FALSE(parse_meminfo("MemTotal: 8388608 kB\nMemFree: 1048576 kB\n").has_value());
}

TEST(ParseMeminfo, ReturnsNulloptWhenAvailableExceedsTotal) {
    EXPECT_FALSE(parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 2000 kB\n").has_value());
}

// ---------- /proc/net/dev ----------

const std::string kNetDevHeader =
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast"
    "|bytes    packets errs drop fifo colls carrier compressed\n";

TEST(ParseNetDev, SumsInterfacesExceptLoopback) {
    const std::string text = kNetDevHeader +
        "    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0\n"
        "  eth0: 1000 10 0 0 0 0 0 0 200 5 0 0 0 0 0 0\n"
        "  eth1: 3000 10 0 0 0 0 0 0 400 5 0 0 0 0 0 0\n";

    auto n = parse_net_dev(text);

    ASSERT_TRUE(n.has_value());
    EXPECT_EQ(n->rx_bytes, 4000u);
    EXPECT_EQ(n->tx_bytes, 600u);
}

TEST(ParseNetDev, HandlesNoSpaceAfterColon) {
    // Large counters can push the number right against the colon.
    const std::string text = kNetDevHeader +
        "  eth0:123456789012 10 0 0 0 0 0 0 42 5 0 0 0 0 0 0\n";

    auto n = parse_net_dev(text);

    ASSERT_TRUE(n.has_value());
    EXPECT_EQ(n->rx_bytes, 123456789012u);
    EXPECT_EQ(n->tx_bytes, 42u);
}

TEST(ParseNetDev, HeaderOnlyGivesZeroCounters) {
    auto n = parse_net_dev(kNetDevHeader);

    ASSERT_TRUE(n.has_value());
    EXPECT_EQ(n->rx_bytes, 0u);
    EXPECT_EQ(n->tx_bytes, 0u);
}

TEST(ParseNetDev, ReturnsNulloptOnEmptyInput) {
    EXPECT_FALSE(parse_net_dev("").has_value());
}

TEST(ParseNetDev, ReturnsNulloptOnTooFewFields) {
    EXPECT_FALSE(parse_net_dev(kNetDevHeader + "  eth0: 1000 10 0\n").has_value());
}

TEST(ParseNetDev, ReturnsNulloptOnLineWithoutColon) {
    EXPECT_FALSE(parse_net_dev(kNetDevHeader + "garbage line\n").has_value());
}
