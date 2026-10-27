// instruments.hpp: the instrument table (securityID -> slot), loaded at startup.
//
// What it does:
//   Holds up to MAX_INSTRUMENTS (book_params.hpp) entries of (securityID,
//   channel), loaded from a plain-text file before any timed code
//   (docs/DESIGN.md 4.4). find() returns an instrument's slot, its position
//   in the file, or kNotFound. The lookup scans the table in order; the RTL
//   does the same lookup as one parallel compare of the securityID against
//   every entry, so both return the first (and only) matching slot.
//
// What it must not do:
//   Allocate after loading, or accept a table it cannot hold exactly: more
//   lines than MAX_INSTRUMENTS, or a securityID twice, is a load error.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>

#include "b3/book_params.hpp"

namespace b3::instruments {

class LoadError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct Entry {
    std::uint64_t security_id = 0;
    std::uint8_t channel = 0;
};

class Table {
public:
    static constexpr std::size_t kNotFound = static_cast<std::size_t>(-1);

    // File format: one "securityID channel" per line; '#' starts a comment;
    // blank lines are ignored. Throws LoadError with the line number.
    static Table load(const std::filesystem::path& path);
    static Table parse(const std::string& text, const std::string& where = "instruments");

    std::size_t find(std::uint64_t security_id) const noexcept {
        for (std::size_t i = 0; i < count_; ++i) {   // RTL: all entries compared at once
            if (entries_[i].security_id == security_id) return i;
        }
        return kNotFound;
    }
    std::size_t size() const noexcept { return count_; }
    const Entry& operator[](std::size_t slot) const noexcept { return entries_[slot]; }

private:
    std::array<Entry, kMaxInstruments> entries_{};
    std::size_t count_ = 0;
};

}  // namespace b3::instruments
