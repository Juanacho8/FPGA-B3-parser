// book_params.hpp: the price-level book's build parameters.
//
// What it does:
//   The single place where the book parameters are set:
//     PUBLISHED_DEPTH (N)  levels per side in each book update (port 2)
//     TRACKED_DEPTH   (K)  levels per side kept internally
//     MAX_INSTRUMENTS      books held
//     BOOK mode            capped (K slots per side, the RTL's algorithm)
//                          or uncapped (no tracked-depth limit, software only)
//   Defaults live here. CMake overrides them with -DB3_PUBLISHED_DEPTH=...,
//   -DB3_TRACKED_DEPTH=..., -DB3_MAX_INSTRUMENTS=..., -DB3_BOOK=capped|uncapped.
//   cpp_baseline/CMakeLists.txt reads the defaults out of this file, so keep
//   the three "#define B3_... <number>" lines below in their current form.
//
// What it must not do:
//   Nothing else in the code may hardcode 5, 16 or 64. Everything reads the
//   constants in namespace b3 below.

#pragma once

#include <cstddef>

// ---- 1. Defaults, used when CMake does not override them ----
#ifndef B3_PUBLISHED_DEPTH
#define B3_PUBLISHED_DEPTH 5
#endif

#ifndef B3_TRACKED_DEPTH
#define B3_TRACKED_DEPTH 16
#endif

#ifndef B3_MAX_INSTRUMENTS
#define B3_MAX_INSTRUMENTS 64
#endif

// B3_BOOK_UNCAPPED: 0 = capped (default), 1 = uncapped. CMake sets it from B3_BOOK.
#ifndef B3_BOOK_UNCAPPED
#define B3_BOOK_UNCAPPED 0
#endif

namespace b3 {

// ---- 2. Constants the rest of the code uses ----
enum class BookMode { Capped, Uncapped };

inline constexpr std::size_t kPublishedDepth = B3_PUBLISHED_DEPTH;
inline constexpr std::size_t kTrackedDepth   = B3_TRACKED_DEPTH;
inline constexpr std::size_t kMaxInstruments = B3_MAX_INSTRUMENTS;
inline constexpr BookMode    kBookMode       = B3_BOOK_UNCAPPED ? BookMode::Uncapped : BookMode::Capped;

// ---- 3. Compile-time checks ----
static_assert(kPublishedDepth > 0, "PUBLISHED_DEPTH must be at least 1");
static_assert(kTrackedDepth > 0, "TRACKED_DEPTH must be at least 1");
static_assert(kMaxInstruments > 0, "MAX_INSTRUMENTS must be at least 1");
static_assert(kPublishedDepth <= kTrackedDepth, "PUBLISHED_DEPTH (N) must not exceed TRACKED_DEPTH (K)");
static_assert(B3_BOOK_UNCAPPED == 0 || B3_BOOK_UNCAPPED == 1, "B3_BOOK_UNCAPPED must be 0 or 1");

}  // namespace b3
