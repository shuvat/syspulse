#pragma once

#include <chrono>
#include <condition_variable>
#include <mutex>

namespace syspulse {

// A stop request that sleeping threads can wait on.
// Unlike sleep_for(), wait_for() returns as soon as stop is requested,
// so shutdown does not have to wait for the end of the current interval.
class StopFlag {
public:
    void request_stop() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopped_ = true;
        }
        cv_.notify_all();
    }

    bool stop_requested() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return stopped_;
    }

    // Waits up to `timeout`. Returns true if stop was requested.
    bool wait_for(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        return cv_.wait_for(lock, timeout, [this] { return stopped_; });
    }

private:
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    bool stopped_ = false;
};

}  // namespace syspulse
