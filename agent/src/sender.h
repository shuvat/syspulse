#pragma once

#include <chrono>
#include <cstdint>
#include <string>
#include <utility>

#include "collector.h"
#include "queue.h"
#include "stop_flag.h"

namespace syspulse {

// Serializes a sample as a single-line JSON object (no trailing newline).
std::string to_json(const Sample& sample);

constexpr std::chrono::milliseconds kInitialBackoff{1000};
constexpr std::chrono::milliseconds kMaxBackoff{30000};

// Exponential backoff: doubles the delay, capped at kMaxBackoff.
std::chrono::milliseconds next_backoff(std::chrono::milliseconds current);

// Owns a socket file descriptor and closes it in the destructor (RAII),
// so no code path can leak the descriptor.
class Socket {
public:
    Socket() = default;
    explicit Socket(int fd) : fd_(fd) {}
    ~Socket() { reset(); }

    // Copying would close the same fd twice; moving transfers ownership.
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    Socket(Socket&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    Socket& operator=(Socket&& other) noexcept {
        if (this != &other) {
            reset();
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }

    bool valid() const { return fd_ >= 0; }
    int fd() const { return fd_; }
    void reset();  // Closes the fd, if any.

private:
    int fd_ = -1;
};

// Resolves `host` (name or IP) and connects. Returns an invalid Socket on failure.
Socket connect_to(const std::string& host, uint16_t port);

// Sends all of `data`, looping over partial writes. Returns false on error.
bool send_all(const Socket& socket, const std::string& data);

struct SenderConfig {
    std::string host;
    uint16_t port = 0;
    std::chrono::milliseconds initial_backoff = kInitialBackoff;
};

// Sender thread body: sends one JSON line per sample over TCP and reconnects
// with backoff on failure. Returns when the queue is closed and drained, or
// when stop is requested while disconnected.
void run_sender(ThreadSafeQueue<Sample>& queue, StopFlag& stop, const SenderConfig& config);

}  // namespace syspulse
