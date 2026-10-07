#include <gtest/gtest.h>
#include <nlohmann/json.hpp>
#include <sstream>
#include <vector>

#include "sender.h"

using namespace syspulse;

namespace {

Sample make_sample(const std::string& host) {
    Sample s;
    s.host = host;
    s.ts = 1760000000;
    s.cpu_percent = 37.5;
    s.mem_used_mb = 2048;
    s.mem_total_mb = 8192;
    s.net_rx_bytes = 123456;
    s.net_tx_bytes = 65432;
    return s;
}

}  // namespace

TEST(ToJson, ContainsAllFields) {
    auto j = nlohmann::json::parse(to_json(make_sample("agent-1")));

    EXPECT_EQ(j["host"], "agent-1");
    EXPECT_EQ(j["ts"], 1760000000);
    EXPECT_DOUBLE_EQ(j["cpu_percent"].get<double>(), 37.5);
    EXPECT_EQ(j["mem_used_mb"], 2048);
    EXPECT_EQ(j["mem_total_mb"], 8192);
    EXPECT_EQ(j["net_rx_bytes"], 123456);
    EXPECT_EQ(j["net_tx_bytes"], 65432);
}

TEST(ToJson, IsSingleLineEvenWithNewlineInHost) {
    // A newline in the output would split one message into two.
    std::string line = to_json(make_sample("bad\nhost"));

    EXPECT_EQ(line.find('\n'), std::string::npos);
    EXPECT_EQ(nlohmann::json::parse(line)["host"], "bad\nhost");
}

TEST(RunSender, WritesOneLinePerSampleUntilClosed) {
    ThreadSafeQueue<Sample> queue(10);
    queue.push(make_sample("a"));
    queue.push(make_sample("b"));
    queue.close();

    std::ostringstream out;
    run_sender(queue, out);  // Returns because the queue is closed and drained.

    std::istringstream lines(out.str());
    std::string line;
    std::vector<std::string> hosts;
    while (std::getline(lines, line)) {
        hosts.push_back(nlohmann::json::parse(line)["host"]);
    }
    EXPECT_EQ(hosts, (std::vector<std::string>{"a", "b"}));
}
