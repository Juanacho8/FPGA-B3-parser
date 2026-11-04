// opt_flags.hpp: the optimised implementation's changes, one switch each.
//
// What it does:
//   Each change of step 10b (docs/bench/optimise.md) has a switch, on by
//   default in an optimised build (B3_IMPL=optimised) and always off in the
//   mirror. A/B measurements build with one switch off
//   (-DB3_OPT_<NAME>=0 in CMAKE_CXX_FLAGS) to time that change alone.
//
// What it must not do:
//   Turn anything on in a mirror build, or change any output.

#pragma once

#include "b3/book_params.hpp"

#ifndef B3_OPT_SINK_INPLACE
#define B3_OPT_SINK_INPLACE 1   // book sink: BOOK_UPDATEs written straight into the output buffer
#endif
#ifndef B3_OPT_BOOK_SCAN
#define B3_OPT_BOOK_SCAN 1      // capped book: best-first scan with early exit; shifts by memmove
#endif
#ifndef B3_OPT_UNCAPPED_SCAN
#define B3_OPT_UNCAPPED_SCAN 1  // uncapped book: best-first scan from the best end before the binary search
#endif

namespace b3::opt {
inline constexpr bool kSinkInPlace = kOptimised && B3_OPT_SINK_INPLACE;
inline constexpr bool kBookScan = kOptimised && B3_OPT_BOOK_SCAN;
inline constexpr bool kUncappedScan = kOptimised && B3_OPT_UNCAPPED_SCAN;
}  // namespace b3::opt
