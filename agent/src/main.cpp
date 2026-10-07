#include <pthread.h>

#include <charconv>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <optional>
#include <string>
#include <thread>

#include "collector.h"
#include "sender.h"

using namespace syspulse;

namespace {

constexpr std::size_t kQueueCapacity = 150;  // 5 minutes of samples at 2s.
constexpr std::chrono::seconds kInterval{2};

std::string env_or(const char* name, const std::string& fallback) {
    const char* value = std::getenv(name);
    return value ? value : fallback;
}

// Returns the port if `text` is a whole number in 1..65535.
std::optional<uint16_t> parse_port(const std::string& text) {
    int value = 0;
    auto [end, ec] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (ec != std::errc() || end != text.data() + text.size() || value < 1 || value > 65535) {
        return std::nullopt;
    }
    return static_cast<uint16_t>(value);
}

std::optional<CpuSource> parse_cpu_source(const std::string& text) {
    if (text == "proc") {
        return CpuSource::Proc;
    }
    if (text == "cgroup") {
        return CpuSource::Cgroup;
    }
    return std::nullopt;
}

}  // namespace

int main() {
    const std::string agent_name = env_or("AGENT_NAME", "agent");

    SenderConfig config;
    config.host = env_or("SERVER_HOST", "127.0.0.1");
    const std::string port_text = env_or("SERVER_PORT", "9000");
    auto port = parse_port(port_text);
    if (!port) {
        std::cerr << "invalid SERVER_PORT: " << port_text << "\n";
        return 1;
    }
    config.port = *port;

    // "proc" (default): CPU of the whole machine. "cgroup": CPU of this
    // container only; set in docker-compose.yml.
    const std::string cpu_source_text = env_or("CPU_SOURCE", "proc");
    auto cpu_source = parse_cpu_source(cpu_source_text);
    if (!cpu_source) {
        std::cerr << "invalid CPU_SOURCE (expected proc or cgroup): " << cpu_source_text << "\n";
        return 1;
    }

    // Block SIGINT/SIGTERM before starting threads. New threads inherit the
    // mask, so no thread is interrupted by these signals; instead, main
    // receives them synchronously with sigwait() below. This avoids a signal
    // handler, where locking a mutex or notifying a condition_variable is not
    // allowed (they are not async-signal-safe).
    sigset_t signals;
    sigemptyset(&signals);
    sigaddset(&signals, SIGINT);
    sigaddset(&signals, SIGTERM);
    pthread_sigmask(SIG_BLOCK, &signals, nullptr);

    ThreadSafeQueue<Sample> queue(kQueueCapacity);
    StopFlag stop;

    // std::thread copies its arguments; std::ref passes a reference instead,
    // so both threads share the same queue and stop flag.
    std::thread collector(run_collector, agent_name, std::ref(queue), std::ref(stop), kInterval,
                          *cpu_source);
    std::thread sender(run_sender, std::ref(queue), std::ref(stop), config);

    std::cerr << "agent " << agent_name << " started, sending to " << config.host << ":"
              << config.port << ", CPU source: " << cpu_source_text << " (Ctrl+C to stop)\n";

    int sig = 0;
    sigwait(&signals, &sig);
    std::cerr << "received " << (sig == SIGINT ? "SIGINT" : "SIGTERM") << ", shutting down\n";

    // Shutdown order matters: stop the producer first, then close the queue,
    // so the sender drains every sample that was already collected.
    stop.request_stop();
    collector.join();
    queue.close();
    sender.join();

    std::cerr << "agent stopped, dropped samples: " << queue.dropped() << "\n";
    return 0;
}
