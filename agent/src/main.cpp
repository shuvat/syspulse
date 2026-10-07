#include <chrono>
#include <cstdlib>
#include <functional>
#include <iostream>
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

}  // namespace

int main() {
    const std::string host = env_or("AGENT_NAME", "agent");

    ThreadSafeQueue<Sample> queue(kQueueCapacity);
    StopFlag stop;

    // std::thread copies its arguments; std::ref passes a reference instead,
    // so both threads share the same queue and stop flag.
    std::thread collector(run_collector, host, std::ref(queue), std::ref(stop), kInterval);
    std::thread sender(run_sender, std::ref(queue), std::ref(std::cout));

    // Temporary: run for 10 seconds. Ctrl+C handling comes in the next step.
    std::this_thread::sleep_for(std::chrono::seconds(10));

    // Shutdown order matters: stop the producer first, then close the queue,
    // so the sender drains every sample that was already collected.
    stop.request_stop();
    collector.join();
    queue.close();
    sender.join();

    std::cerr << "agent stopped, dropped samples: " << queue.dropped() << "\n";
    return 0;
}
