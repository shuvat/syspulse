cmake_minimum_required(VERSION 3.20)
project(syspulse_agent LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Default to Debug so GDB and valgrind get symbols.
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()

# Applied per target, so third-party code is not compiled with our warnings.
set(SYSPULSE_WARNINGS -Wall -Wextra -Wpedantic)

include(FetchContent)

# Pinned to a release so every build uses the same code.
FetchContent_Declare(
    nlohmann_json
    URL https://github.com/nlohmann/json/releases/download/v3.12.0/json.tar.xz
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
)
FetchContent_MakeAvailable(nlohmann_json)

find_package(Threads REQUIRED)

# Core logic as a library, so the tests can link against it.
add_library(syspulse_core STATIC src/collector.cpp src/sender.cpp)
target_include_directories(syspulse_core PUBLIC src)
# nlohmann_json is PRIVATE: it is used only inside sender.cpp, not in headers.
target_link_libraries(syspulse_core PUBLIC Threads::Threads PRIVATE nlohmann_json::nlohmann_json)
target_compile_options(syspulse_core PRIVATE ${SYSPULSE_WARNINGS})

add_executable(syspulse_agent src/main.cpp)
target_link_libraries(syspulse_agent PRIVATE syspulse_core)
target_compile_options(syspulse_agent PRIVATE ${SYSPULSE_WARNINGS})

# Defines the BUILD_TESTING option (ON by default) and calls enable_testing().
# Configure with -DBUILD_TESTING=OFF to skip downloading GoogleTest.
include(CTest)

if(BUILD_TESTING)
    # We only use gtest; skip building gmock and the install rules.
    set(BUILD_GMOCK OFF CACHE BOOL "" FORCE)
    set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)

    FetchContent_Declare(
        googletest
        URL https://github.com/google/googletest/archive/refs/tags/v1.17.0.tar.gz
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    )
    FetchContent_MakeAvailable(googletest)

    add_executable(agent_tests
        tests/test_parsers.cpp
        tests/test_queue.cpp
        tests/test_sender.cpp
        tests/test_collector_thread.cpp
    )
    target_link_libraries(agent_tests PRIVATE syspulse_core nlohmann_json::nlohmann_json GTest::gtest_main)
    target_compile_options(agent_tests PRIVATE ${SYSPULSE_WARNINGS})

    # Registers each TEST() as a separate ctest test. PRE_TEST lists the tests
    # when ctest runs instead of right after linking, so a test binary that
    # cannot start (e.g. a sanitizer build) fails the test run, not the build.
    include(GoogleTest)
    gtest_discover_tests(agent_tests DISCOVERY_MODE PRE_TEST)
endif()
