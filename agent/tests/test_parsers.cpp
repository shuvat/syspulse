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

TEST(CgroupCpuPercent, RelativeToFractionalLimit) {
    // Limit of half a CPU: 0.5s used in 1s is the whole capacity.
    EXPECT_DOUBLE_EQ(cgroup_cpu_percent(0, 500'000, 1'000'000, 0.5), 100.0);
}

// ---------- cgroup v2 memory ----------

TEST(ParseCgroupStatField, FindsKeyInMemoryStat) {
    // Real lines from /sys/fs/cgroup/memory.stat inside agent-1.
    const std::string text =
        "anon 557056\n"
        "file 1323008\n"
        "inactive_file 847872\n"
        "active_file 475136\n";
    EXPECT_EQ(parse_cgroup_stat_field(text, "inactive_file"), 847872u);
}

TEST(ParseCgroupStatField, KeyMustMatchExactly) {
    // "file" must not match "inactive_file" or "active_file".
    EXPECT_EQ(parse_cgroup_stat_field("inactive_file 5\nfile 7\n", "file"), 7u);
    EXPECT_FALSE(parse_cgroup_stat_field("inactive_file 5\n", "file").has_value());
}

TEST(ParseCgroupNumber, ReadsSingleValue) {
    EXPECT_EQ(parse_cgroup_number("4370432\n"), 4370432u);  // memory.current
    EXPECT_EQ(parse_cgroup_number("268435456\n"), 268435456u);  // memory.max = 256 MiB
}

TEST(ParseCgroupNumber, MaxAndMalformedAreNullopt) {
    EXPECT_FALSE(parse_cgroup_number("max\n").has_value());  // no limit
    EXPECT_FALSE(parse_cgroup_number("").has_value());
    EXPECT_FALSE(parse_cgroup_number("12ab\n").has_value());
    EXPECT_FALSE(parse_cgroup_number("-5\n").has_value());
}

constexpr uint64_t kMiB = 1024 * 1024;

TEST(CgroupMemInfo, SubtractsReclaimableCache) {
    // 100 MiB charged, of which 40 MiB is inactive page cache -> 60 MiB used.
    auto info = cgroup_mem_info(100 * kMiB, 40 * kMiB, 256 * kMiB, 8192 * kMiB);
    EXPECT_EQ(info.used_mb, 60u);
    EXPECT_EQ(info.total_mb, 256u);  // The limit, not the machine.
}

TEST(CgroupMemInfo, NoLimitUsesMachineTotal) {
    auto info = cgroup_mem_info(100 * kMiB, 0, std::nullopt, 8192 * kMiB);
    EXPECT_EQ(info.total_mb, 8192u);
}

TEST(CgroupMemInfo, LimitAboveMachineIsCapped) {
    auto info = cgroup_mem_info(100 * kMiB, 0, 16384 * kMiB, 8192 * kMiB);
    EXPECT_EQ(info.total_mb, 8192u);
}

TEST(CgroupMemInfo, InactiveLargerThanCurrentGivesZeroNotWrapAround) {
    // The files are read at different instants; unsigned subtraction must not wrap.
    auto info = cgroup_mem_info(10 * kMiB, 12 * kMiB, 256 * kMiB, 8192 * kMiB);
    EXPECT_EQ(info.used_mb, 0u);
}

TEST(CgroupMemInfo, UsedNeverExceedsTotal) {
    auto info = cgroup_mem_info(300 * kMiB, 0, 256 * kMiB, 8192 * kMiB);
    EXPECT_EQ(info.used_mb, 256u);
}

// ---------- cgroup v2 cpu.max ----------

TEST(ParseCgroupCpuLimit, OneCpu) {
    EXPECT_EQ(parse_cgroup_cpu_limit("100000 100000\n"), 1.0);
}

TEST(ParseCgroupCpuLimit, FractionalCpus) {
    // docker run --cpus=1.5
    EXPECT_EQ(parse_cgroup_cpu_limit("150000 100000\n"), 1.5);
}

TEST(ParseCgroupCpuLimit, MaxMeansNoLimit) {
    // Real content without a limit (read inside agent-1 before limits were set).
    EXPECT_FALSE(parse_cgroup_cpu_limit("max 100000\n").has_value());
}

TEST(ParseCgroupCpuLimit, ReturnsNulloptOnMalformedInput) {
    EXPECT_FALSE(parse_cgroup_cpu_limit("").has_value());
    EXPECT_FALSE(parse_cgroup_cpu_limit("100000\n").has_value());      // No period.
    EXPECT_FALSE(parse_cgroup_cpu_limit("abc 100000\n").has_value());  // Quota not a number.
    EXPECT_FALSE(parse_cgroup_cpu_limit("100000 0\n").has_value());    // Division by zero.
    EXPECT_FALSE(parse_cgroup_cpu_limit("0 100000\n").has_value());    // Zero capacity.
}

// ---------- effective_cpu_capacity ----------

TEST(EffectiveCpuCapacity, UsesLimitBelowCpuCount) {
    EXPECT_DOUBLE_EQ(effective_cpu_capacity(1.0, 8), 1.0);
}

TEST(EffectiveCpuCapacity, AllCpusWithoutLimit) {
    EXPECT_DOUBLE_EQ(effective_cpu_capacity(std::nullopt, 8), 8.0);
}

TEST(EffectiveCpuCapacity, LimitAboveCpuCountIsCapped) {
    // --cpus=16 on an 8-CPU machine: at most 8 CPUs can actually be used.
    EXPECT_DOUBLE_EQ(effective_cpu_capacity(16.0, 8), 8.0);
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
