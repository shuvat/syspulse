#include <gtest/gtest.h>

#include <chrono>
#include <thread>
#include <vector>

#include "queue.h"
#include "stop_flag.h"

using namespace syspulse;
using namespace std::chrono_literals;

// ---------- ThreadSafeQueue ----------

TEST(ThreadSafeQueue, PopsInFifoOrder) {
    ThreadSafeQueue<int> queue(10);
    queue.push(1);
    queue.push(2);
    queue.push(3);

    EXPECT_EQ(queue.pop(), 1);
    EXPECT_EQ(queue.pop(), 2);
    EXPECT_EQ(queue.pop(), 3);
}

TEST(ThreadSafeQueue, DropsOldestWhenFull) {
    ThreadSafeQueue<int> queue(3);
    for (int i = 1; i <= 5; ++i) {
        queue.push(i);
    }

    EXPECT_EQ(queue.size(), 3u);
    EXPECT_EQ(queue.dropped(), 2u);
    EXPECT_EQ(queue.pop(), 3);
    EXPECT_EQ(queue.pop(), 4);
    EXPECT_EQ(queue.pop(), 5);
}

TEST(ThreadSafeQueue, PopBlocksUntilItemIsPushed) {
    ThreadSafeQueue<int> queue(10);
    std::optional<int> result;

    std::thread consumer([&] { result = queue.pop(); });
    std::this_thread::sleep_for(50ms);  // Give the consumer time to block.
    queue.push(42);
    consumer.join();

    EXPECT_EQ(result, 42);
}

TEST(ThreadSafeQueue, CloseWakesBlockedConsumer) {
    ThreadSafeQueue<int> queue(10);
    std::optional<int> result = 0;

    std::thread consumer([&] { result = queue.pop(); });
    std::this_thread::sleep_for(50ms);
    queue.close();
    consumer.join();  // Would hang forever if close() did not notify.

    EXPECT_FALSE(result.has_value());
}

TEST(ThreadSafeQueue, CloseStillDrainsRemainingItems) {
    ThreadSafeQueue<int> queue(10);
    queue.push(1);
    queue.push(2);
    queue.close();

    EXPECT_EQ(queue.pop(), 1);
    EXPECT_EQ(queue.pop(), 2);
    EXPECT_FALSE(queue.pop().has_value());
}

TEST(ThreadSafeQueue, RejectsPushAfterClose) {
    ThreadSafeQueue<int> queue(10);
    queue.close();

    EXPECT_FALSE(queue.push(1));
    EXPECT_EQ(queue.size(), 0u);
}

TEST(ThreadSafeQueue, ManyProducersLoseNoItems) {
    constexpr int kProducers = 4;
    constexpr int kItemsPerProducer = 10000;
    // Large enough that nothing is dropped.
    ThreadSafeQueue<int> queue(kProducers * kItemsPerProducer);

    long long consumed_sum = 0;
    int consumed_count = 0;
    std::thread consumer([&] {
        while (auto item = queue.pop()) {
            consumed_sum += *item;
            ++consumed_count;
        }
    });

    std::vector<std::thread> producers;
    for (int p = 0; p < kProducers; ++p) {
        producers.emplace_back([&] {
            for (int i = 1; i <= kItemsPerProducer; ++i) {
                queue.push(i);
            }
        });
    }
    for (auto& t : producers) {
        t.join();
    }
    queue.close();
    consumer.join();

    const long long expected_sum =
        kProducers * (static_cast<long long>(kItemsPerProducer) * (kItemsPerProducer + 1) / 2);
    EXPECT_EQ(consumed_count, kProducers * kItemsPerProducer);
    EXPECT_EQ(consumed_sum, expected_sum);
}

// ---------- StopFlag ----------

TEST(StopFlag, WaitTimesOutWhenNotStopped) {
    StopFlag stop;
    EXPECT_FALSE(stop.wait_for(10ms));
    EXPECT_FALSE(stop.stop_requested());
}

TEST(StopFlag, RequestStopWakesWaiterEarly) {
    StopFlag stop;
    bool stopped = false;
    auto start = std::chrono::steady_clock::now();

    std::thread waiter([&] { stopped = stop.wait_for(10s); });
    std::this_thread::sleep_for(50ms);
    stop.request_stop();
    waiter.join();

    auto elapsed = std::chrono::steady_clock::now() - start;
    EXPECT_TRUE(stopped);
    EXPECT_LT(elapsed, 5s);  // Far below the 10s timeout.
}
