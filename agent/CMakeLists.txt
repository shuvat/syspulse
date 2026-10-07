cmake_minimum_required(VERSION 3.20)
project(syspulse_agent LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Default to Debug so GDB and valgrind get symbols.
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()

# Core logic as a library, so the tests can link against it later.
add_library(syspulse_core STATIC src/collector.cpp)
target_include_directories(syspulse_core PUBLIC src)
target_compile_options(syspulse_core PRIVATE -Wall -Wextra -Wpedantic)

add_executable(syspulse_agent src/main.cpp)
target_link_libraries(syspulse_agent PRIVATE syspulse_core)
target_compile_options(syspulse_agent PRIVATE -Wall -Wextra -Wpedantic)

# Defines the BUILD_TESTING option (ON by default) and calls enable_testing().
# Configure with -DBUILD_TESTING=OFF to skip downloading GoogleTest.
include(CTest)

if(BUILD_TESTING)
    include(FetchContent)
    # Pinned to a release so every build tests against the same code.
    FetchContent_Declare(
        googletest
        URL https://github.com/google/googletest/archive/refs/tags/v1.17.0.tar.gz
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    )
    FetchContent_MakeAvailable(googletest)

    add_executable(agent_tests tests/test_parsers.cpp)
    target_link_libraries(agent_tests PRIVATE syspulse_core GTest::gtest_main)
    target_compile_options(agent_tests PRIVATE -Wall -Wextra -Wpedantic)

    # Registers each TEST() as a separate ctest test.
    include(GoogleTest)
    gtest_discover_tests(agent_tests)
endif()
