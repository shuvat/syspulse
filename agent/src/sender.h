#pragma once

#include <ostream>
#include <string>

#include "collector.h"
#include "queue.h"

namespace syspulse {

// Serializes a sample as a single-line JSON object (no trailing newline).
std::string to_json(const Sample& sample);

// Sender thread body (temporary): writes one JSON line per sample to `out`
// until the queue is closed and drained. Will become a TCP sender.
void run_sender(ThreadSafeQueue<Sample>& queue, std::ostream& out);

}  // namespace syspulse
