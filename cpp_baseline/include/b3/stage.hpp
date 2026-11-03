// stage.hpp: stage boundaries for profiling.
//
// What it does:
//   B3_STAGE marks each pipeline stage's entry point. In a profile build
//   (CMake B3_PROFILE=ON) it is noinline, so a sampling profiler can say
//   which stage the time goes to (packet decoder, walker, field decode, event
//   sink, tracker and its field guards, book sink, book); each mark adds a
//   call. Otherwise it is empty and the compiler inlines as it likes.
//
// What it must not do:
//   Change any output.

#pragma once

#if defined(B3_PROFILE) && B3_PROFILE
#define B3_STAGE __attribute__((noinline))
#else
#define B3_STAGE
#endif
