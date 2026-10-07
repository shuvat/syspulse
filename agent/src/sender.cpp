#include "sender.h"

#include <netdb.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <iostream>
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

std::chrono::milliseconds next_backoff(std::chrono::milliseconds current) {
    return std::min(current * 2, kMaxBackoff);
}

void Socket::reset() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

Socket connect_to(const std::string& host, uint16_t port) {
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;  // IPv4 or IPv6.
    hints.ai_socktype = SOCK_STREAM;

    addrinfo* results = nullptr;
    const std::string port_str = std::to_string(port);
    int rc = ::getaddrinfo(host.c_str(), port_str.c_str(), &hints, &results);
    if (rc != 0) {
        std::cerr << "sender: cannot resolve " << host << ": " << ::gai_strerror(rc) << "\n";
        return Socket();
    }

    // A name can resolve to several addresses; use the first one that connects.
    Socket connected;
    for (addrinfo* ai = results; ai != nullptr; ai = ai->ai_next) {
        Socket candidate(::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol));
        if (!candidate.valid()) {
            continue;
        }
        // On Linux SO_SNDTIMEO also limits connect(); without it, connecting
        // to an address that never answers can block for about two minutes.
        timeval timeout{};
        timeout.tv_sec = 5;
        ::setsockopt(candidate.fd(), SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

        if (::connect(candidate.fd(), ai->ai_addr, ai->ai_addrlen) == 0) {
            connected = std::move(candidate);
            break;
        }
        // On failure, `candidate` closes its fd when it goes out of scope.
    }
    ::freeaddrinfo(results);
    return connected;
}

bool send_all(const Socket& socket, const std::string& data) {
    std::size_t sent = 0;
    while (sent < data.size()) {
        // MSG_NOSIGNAL: if the peer closed the connection, return EPIPE instead
        // of raising SIGPIPE, whose default action kills the process.
        ssize_t n = ::send(socket.fd(), data.data() + sent, data.size() - sent, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) {
                continue;  // Interrupted before sending anything; just retry.
            }
            return false;
        }
        sent += static_cast<std::size_t>(n);  // send() may write only part of the buffer.
    }
    return true;
}

void run_sender(ThreadSafeQueue<Sample>& queue, StopFlag& stop, const SenderConfig& config) {
    Socket conn;
    auto backoff = config.initial_backoff;

    while (auto sample = queue.pop()) {
        const std::string line = to_json(*sample) + "\n";

        // Keep retrying this sample, so a reconnect does not skip it.
        while (true) {
            if (!conn.valid()) {
                // Shutting down with no connection: give up on the rest
                // instead of retrying forever.
                if (stop.stop_requested()) {
                    return;
                }
                conn = connect_to(config.host, config.port);
                if (!conn.valid()) {
                    std::cerr << "sender: cannot connect to " << config.host << ":" << config.port
                              << ", retrying in " << backoff.count() << " ms\n";
                    if (stop.wait_for(backoff)) {
                        return;
                    }
                    backoff = next_backoff(backoff);
                    continue;
                }
                std::cerr << "sender: connected to " << config.host << ":" << config.port << "\n";
                backoff = config.initial_backoff;
            }

            if (send_all(conn, line)) {
                break;
            }
            std::cerr << "sender: send failed, reconnecting\n";
            conn.reset();
        }
    }
}

}  // namespace syspulse
