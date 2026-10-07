#pragma once

#include <algorithm>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>
#include <optional>

namespace syspulse {

// Bounded multi-producer / multi-consumer queue.
// When full, push() drops the oldest item: for monitoring, fresh data is
// worth more than old data, and memory must not grow while the server is down.
template <typename T>
class ThreadSafeQueue {
public:
    explicit ThreadSafeQueue(std::size_t capacity) : capacity_(std::max<std::size_t>(capacity, 1)) {}

    // A mutex cannot be copied, so neither can the queue.
    ThreadSafeQueue(const ThreadSafeQueue&) = delete;
    ThreadSafeQueue& operator=(const ThreadSafeQueue&) = delete;

    // Returns false if the queue is closed (the item is not added).
    bool push(T item) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (closed_) {
                return false;
            }
            if (items_.size() >= capacity_) {
                items_.pop_front();
                ++dropped_;
            }
            items_.push_back(std::move(item));
        }
        // Notify after unlocking, so the woken thread does not immediately
        // block again on a mutex we still hold.
        cv_.notify_one();
        return true;
    }

    // Blocks until an item is available or the queue is closed.
    // After close(), remaining items are still returned; then std::nullopt.
    std::optional<T> pop() {
        std::unique_lock<std::mutex> lock(mutex_);
        // The predicate protects against spurious wakeups: wait() may return
        // even though nobody notified, so we re-check the real condition.
        cv_.wait(lock, [this] { return closed_ || !items_.empty(); });
        if (items_.empty()) {
            return std::nullopt;
        }
        T item = std::move(items_.front());
        items_.pop_front();
        return item;
    }

    // Wakes all waiting consumers. No new items are accepted after this.
    void close() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            closed_ = true;
        }
        cv_.notify_all();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return items_.size();
    }

    std::size_t dropped() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return dropped_;
    }

private:
    mutable std::mutex mutex_;  // mutable: locked in const getters too.
    std::condition_variable cv_;
    std::deque<T> items_;
    const std::size_t capacity_;
    std::size_t dropped_ = 0;
    bool closed_ = false;
};

}  // namespace syspulse
