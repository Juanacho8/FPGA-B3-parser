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
#include "b3/opt_flags.hpp"

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
    // The lookup the pipeline uses: find() in a mirror build; in an optimised
    // build (opt::kLookup) an open-addressing hash index built at load, one
    // probe for most securityIDs. The same slot either way.
    std::size_t lookup(std::uint64_t security_id) const noexcept {
        if constexpr (opt::kLookup) {
            for (std::size_t i = hash(security_id);; i = (i + 1) & (kIndexSize - 1)) {
                const std::uint8_t at = index_[i];
                if (at == kEmpty) return kNotFound;
                if (entries_[at].security_id == security_id) return at;
            }
        } else {
            return find(security_id);
        }
    }
    void build_index() noexcept {
        index_.fill(kEmpty);
        for (std::size_t slot = 0; slot < count_; ++slot) {
            std::size_t i = hash(entries_[slot].security_id);
            while (index_[i] != kEmpty) i = (i + 1) & (kIndexSize - 1);
            index_[i] = static_cast<std::uint8_t>(slot);
        }
    }

    std::size_t size() const noexcept { return count_; }
    const Entry& operator[](std::size_t slot) const noexcept { return entries_[slot]; }

private:
    static constexpr std::size_t kIndexSize = [] {
        std::size_t n = 8;
        while (n < 4 * kMaxInstruments) n *= 2;
        return n;
    }();
    static constexpr std::uint8_t kEmpty = 0xff;
    static_assert(kMaxInstruments < 0xff, "index entries are one byte");
    static std::size_t hash(std::uint64_t id) noexcept {
        return static_cast<std::size_t>((id * 0x9e3779b97f4a7c15ULL) >> 40) & (kIndexSize - 1);
    }

    std::array<Entry, kMaxInstruments> entries_{};
    std::size_t count_ = 0;
    std::array<std::uint8_t, kIndexSize> index_{};   // optimised lookup only
};

}  // namespace b3::instruments
