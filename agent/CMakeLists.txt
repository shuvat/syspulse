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
