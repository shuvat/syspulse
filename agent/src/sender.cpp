#include "sender.h"

#include <nlohmann/json.hpp>

namespace syspulse {

std::string to_json(const Sample& sample) {
    nlohmann::json j = {
        {"host", sample.host},
        {"ts", sample.ts},
        {"cpu_percent", sample.cpu_percent},
        {"mem_used_mb", sample.mem_used_mb},
        {"mem_total_mb", sample.mem_total_mb},
        {"net_rx_bytes", sample.net_rx_bytes},
        {"net_tx_bytes", sample.net_tx_bytes},
    };
    // dump() without indentation produces one line; newlines inside strings
    // are escaped as "\n", so the output can never break our line framing.
    return j.dump();
}

void run_sender(ThreadSafeQueue<Sample>& queue, std::ostream& out) {
    // pop() returns std::nullopt only after close() and once the queue is empty.
    while (auto sample = queue.pop()) {
        out << to_json(*sample) << '\n' << std::flush;
    }
}

}  // namespace syspulse
