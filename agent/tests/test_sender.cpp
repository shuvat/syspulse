#include <gtest/gtest.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>

#include <atomic>
#include <chrono>
#include <functional>
#include <nlohmann/json.hpp>
#include <thread>

#include "sender.h"

using namespace syspulse;
using namespace std::chrono_literals;

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

// A minimal TCP server on 127.0.0.1 with a port chosen by the OS.
class TestServer {
public:
    TestServer() : listener_(::socket(AF_INET, SOCK_STREAM, 0)) {
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port = 0;  // Port 0: let the OS pick a free port.
        ::bind(listener_.fd(), reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
        ::listen(listener_.fd(), 1);

        socklen_t len = sizeof(addr);
        ::getsockname(listener_.fd(), reinterpret_cast<sockaddr*>(&addr), &len);
        port_ = ntohs(addr.sin_port);

        // On Linux SO_RCVTIMEO also limits accept(), so a broken sender
        // fails the test instead of hanging it.
        timeval timeout{};
        timeout.tv_sec = 5;
        ::setsockopt(listener_.fd(), SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    }

    uint16_t port() const { return port_; }

    Socket accept_client() {
        Socket client(::accept(listener_.fd(), nullptr, nullptr));
        if (client.valid()) {
            timeval timeout{};
            timeout.tv_sec = 5;
            ::setsockopt(client.fd(), SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        }
        return client;
    }

private:
    Socket listener_;
    uint16_t port_ = 0;
};

// Reads bytes until '\n' (not included). Slow but simple; fine for tests.
std::string read_line(const Socket& socket) {
    std::string line;
    char c = 0;
    while (::recv(socket.fd(), &c, 1, 0) == 1 && c != '\n') {
        line += c;
    }
    return line;
}

std::string host_of(const std::string& line) {
    return nlohmann::json::parse(line)["host"];
}

}  // namespace

// ---------- to_json ----------

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

// ---------- next_backoff ----------

TEST(NextBackoff, DoublesUntilCap) {
    EXPECT_EQ(next_backoff(1000ms), 2000ms);
    EXPECT_EQ(next_backoff(8000ms), 16000ms);
    EXPECT_EQ(next_backoff(16000ms), kMaxBackoff);  // 32s capped to 30s.
    EXPECT_EQ(next_backoff(kMaxBackoff), kMaxBackoff);
}

// ---------- run_sender over real TCP ----------

TEST(RunSender, SendsJsonLinesToServer) {
    TestServer server;
    ThreadSafeQueue<Sample> queue(10);
    StopFlag stop;
    queue.push(make_sample("a"));
    queue.push(make_sample("b"));

    SenderConfig config{"127.0.0.1", server.port(), 50ms};
    std::thread sender(run_sender, std::ref(queue), std::ref(stop), config);

    Socket client = server.accept_client();
    ASSERT_TRUE(client.valid());
    EXPECT_EQ(host_of(read_line(client)), "a");
    EXPECT_EQ(host_of(read_line(client)), "b");

    stop.request_stop();
    queue.close();
    sender.join();
}

TEST(RunSender, ReconnectsAfterServerClosesConnection) {
    TestServer server;
    ThreadSafeQueue<Sample> queue(10);
    StopFlag stop;
    queue.push(make_sample("before"));

    SenderConfig config{"127.0.0.1", server.port(), 50ms};
    std::thread sender(run_sender, std::ref(queue), std::ref(stop), config);

    {
        Socket first = server.accept_client();
        ASSERT_TRUE(first.valid());
        EXPECT_EQ(host_of(read_line(first)), "before");
    }  // `first` closes here: the server drops the connection.

    // The first send after the drop may still "succeed" into the kernel buffer
    // (and be lost), so keep pushing until the sender notices and reconnects.
    std::atomic<bool> done{false};
    std::thread pusher([&] {
        while (!done) {
            queue.push(make_sample("after"));
            std::this_thread::sleep_for(20ms);
        }
    });

    Socket second = server.accept_client();
    ASSERT_TRUE(second.valid());
    EXPECT_EQ(host_of(read_line(second)), "after");

    done = true;
    pusher.join();
    stop.request_stop();
    queue.close();
    sender.join();
}

TEST(RunSender, StopsPromptlyWhenServerIsDown) {
    uint16_t closed_port = 0;
    {
        TestServer temp;
        closed_port = temp.port();
    }  // Nothing listens on this port anymore, so connect() is refused.

    ThreadSafeQueue<Sample> queue(10);
    StopFlag stop;
    queue.push(make_sample("a"));

    // A long backoff: the test passes only if stop interrupts the wait.
    SenderConfig config{"127.0.0.1", closed_port, 10s};
    std::thread sender(run_sender, std::ref(queue), std::ref(stop), config);
    std::this_thread::sleep_for(100ms);

    auto start = std::chrono::steady_clock::now();
    stop.request_stop();
    queue.close();
    sender.join();

    EXPECT_LT(std::chrono::steady_clock::now() - start, 2s);
}
