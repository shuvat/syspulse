# Design Decisions

## Message format: newline-delimited JSON over TCP
- Each sample is one JSON object on one line, ending with "\n".
- Why: easy to parse in both C++ and Python, human-readable for debugging,
  and the newline makes it simple to split a TCP stream into messages.

## Transport: TCP (not UDP)
- Why: metrics must arrive in order and without loss for anomaly detection.
  TCP handles retransmission; the agent reconnects if the server goes down.

## Agent threading: producer/consumer queue
- One thread collects samples every 2s, another sends them.
- Why: slow network should not delay sampling. Queue is protected by
  std::mutex + std::condition_variable.

## Database: PostgreSQL
- Why: (fill in on Day 2)

## AI integration: MCP server
- Why: (fill in on Day 4)