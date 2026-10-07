# Multi-stage build: compile in a full toolchain image, ship only the binary.

# ---- Build stage ----
FROM ubuntu:24.04 AS build

# ca-certificates: FetchContent downloads nlohmann/json over HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Tests are not built here (BUILD_TESTING=OFF), so tests/ is not needed.
COPY CMakeLists.txt .
COPY src/ src/

RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
    && cmake --build build --target syspulse_agent -j"$(nproc)"

# ---- Runtime stage ----
FROM ubuntu:24.04

# Run as an unprivileged user; reading /proc and opening a TCP connection need no root.
RUN useradd --system --no-create-home agent
COPY --from=build /src/build/syspulse_agent /usr/local/bin/syspulse_agent
USER agent

# Exec form (JSON array): the agent runs as PID 1 and receives the SIGTERM
# from `docker stop` directly, without a shell in between.
ENTRYPOINT ["/usr/local/bin/syspulse_agent"]
