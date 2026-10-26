# C++ baseline build.
#
# Build types:
#   Release  -O3 plus the native CPU flag (benchmarks, golden files)
#   Debug    -O1 -g with AddressSanitizer and UndefinedBehaviorSanitizer (tests)
#
# Book parameters (defaults live in include/b3/book_params.hpp):
#   -DB3_PUBLISHED_DEPTH=N  -DB3_TRACKED_DEPTH=K  -DB3_MAX_INSTRUMENTS=M
#   -DB3_BOOK=capped|uncapped
# Leave a parameter unset to use the header's default.

cmake_minimum_required(VERSION 3.20)
project(b3_baseline LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Release or Debug" FORCE)
endif()

# ---- 1. Warnings, as errors, on everything we compile ----
add_compile_options(-Wall -Wextra -Wpedantic -Werror)

# ---- 2. Release: -O3 and the native CPU flag ----
# Apple clang on Apple Silicon accepts -mcpu=native (the AArch64 spelling);
# fall back to -march=native for x86 compilers.
include(CheckCXXCompilerFlag)
check_cxx_compiler_flag(-mcpu=native B3_HAS_MCPU_NATIVE)
check_cxx_compiler_flag(-march=native B3_HAS_MARCH_NATIVE)
if(B3_HAS_MCPU_NATIVE)
  set(B3_NATIVE_FLAG -mcpu=native)
elseif(B3_HAS_MARCH_NATIVE)
  set(B3_NATIVE_FLAG -march=native)
else()
  message(FATAL_ERROR "Compiler accepts neither -mcpu=native nor -march=native")
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -DNDEBUG")
add_compile_options($<$<CONFIG:Release>:${B3_NATIVE_FLAG}>)

# ---- 3. Debug: AddressSanitizer + UndefinedBehaviorSanitizer ----
# -fno-sanitize-recover makes any UB report abort, so tests fail on it.
set(CMAKE_CXX_FLAGS_DEBUG "-O1 -g")
set(B3_SANITIZE -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
add_compile_options("$<$<CONFIG:Debug>:${B3_SANITIZE}>")
add_link_options("$<$<CONFIG:Debug>:${B3_SANITIZE}>")

# ---- 4. Book parameters ----
# Read the defaults out of book_params.hpp so it stays the only place they are set.
set(B3_PARAMS_HEADER ${CMAKE_CURRENT_SOURCE_DIR}/include/b3/book_params.hpp)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${B3_PARAMS_HEADER})
file(STRINGS ${B3_PARAMS_HEADER} B3_PARAM_LINES REGEX "^#define B3_[A-Z_]+ [0-9]+$")

set(B3_PUBLISHED_DEPTH "" CACHE STRING "Levels per side in each book update (empty = header default)")
set(B3_TRACKED_DEPTH   "" CACHE STRING "Levels per side kept internally (empty = header default)")
set(B3_MAX_INSTRUMENTS "" CACHE STRING "Books held (empty = header default)")
set(B3_BOOK capped CACHE STRING "Book mode: capped or uncapped")
set_property(CACHE B3_BOOK PROPERTY STRINGS capped uncapped)

set(B3_PARAM_DEFINES "")
foreach(name B3_PUBLISHED_DEPTH B3_TRACKED_DEPTH B3_MAX_INSTRUMENTS)
  # Header default
  set(default "")
  foreach(line IN LISTS B3_PARAM_LINES)
    if(line MATCHES "^#define ${name} ([0-9]+)$")
      set(default ${CMAKE_MATCH_1})
    endif()
  endforeach()
  if(default STREQUAL "")
    message(FATAL_ERROR "No default for ${name} found in ${B3_PARAMS_HEADER}")
  endif()
  # Override from the command line, if any
  if(NOT "${${name}}" STREQUAL "")
    if(NOT "${${name}}" MATCHES "^[1-9][0-9]*$")
      message(FATAL_ERROR "${name} must be a positive integer, got '${${name}}'")
    endif()
    set(value ${${name}})
    list(APPEND B3_PARAM_DEFINES ${name}=${value})
  else()
    set(value ${default})
  endif()
  set(${name}_EFFECTIVE ${value})
endforeach()

if(B3_BOOK STREQUAL "capped")
  set(B3_BOOK_UNCAPPED 0)
elseif(B3_BOOK STREQUAL "uncapped")
  set(B3_BOOK_UNCAPPED 1)
else()
  message(FATAL_ERROR "B3_BOOK must be 'capped' or 'uncapped', got '${B3_BOOK}'")
endif()
list(APPEND B3_PARAM_DEFINES B3_BOOK_UNCAPPED=${B3_BOOK_UNCAPPED})

message(STATUS "b3: ${CMAKE_BUILD_TYPE}, N=${B3_PUBLISHED_DEPTH_EFFECTIVE} "
               "K=${B3_TRACKED_DEPTH_EFFECTIVE} instruments=${B3_MAX_INSTRUMENTS_EFFECTIVE} "
               "book=${B3_BOOK}")

# ---- 5. Library ----
add_library(b3 STATIC src/pcap.cpp)
target_include_directories(b3 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_definitions(b3 PUBLIC ${B3_PARAM_DEFINES})

# ---- 6. Apps ----
add_executable(b3_pcapinfo apps/b3_pcapinfo.cpp)
target_link_libraries(b3_pcapinfo PRIVATE b3)
add_executable(b3_decode apps/b3_decode.cpp)
target_link_libraries(b3_decode PRIVATE b3)

# ---- 7. Tests ----
enable_testing()
add_executable(b3_tests tests/test_build.cpp tests/test_layouts.cpp tests/test_wire.cpp
  tests/test_pcap.cpp tests/test_walker.cpp)
target_link_libraries(b3_tests PRIVATE b3)
target_include_directories(b3_tests SYSTEM PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
# What CMake configured, so the test can check the code saw the same values.
target_compile_definitions(b3_tests PRIVATE
  B3_EXPECT_PUBLISHED_DEPTH=${B3_PUBLISHED_DEPTH_EFFECTIVE}
  B3_EXPECT_TRACKED_DEPTH=${B3_TRACKED_DEPTH_EFFECTIVE}
  B3_EXPECT_MAX_INSTRUMENTS=${B3_MAX_INSTRUMENTS_EFFECTIVE}
  B3_EXPECT_BOOK_UNCAPPED=${B3_BOOK_UNCAPPED})
add_test(NAME b3_tests COMMAND b3_tests)
