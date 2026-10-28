// records.hpp: the port 1 event record and the output file header.
//
// What it does:
//   The 64-byte port 1 record (docs/DESIGN.md 3.1) and the 32-byte file
//   header of events.bin and book.bin (DESIGN.md 2) as fixed-size structs,
//   with static_asserts on the size and every field offset, plus the kind,
//   flag and side numbers of DESIGN.md 3.2 and 3.3 as named constants.
//   The structs are written to disk as their bytes, so the build requires a
//   little-endian host: on any other, this file stops the build.
//
// What it must not do:
//   Hold logic. What goes in each field, per kind, is event_sink.hpp's.

#pragma once

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>

#include "b3/book_params.hpp"

namespace b3::records {

static_assert(std::endian::native == std::endian::little,
              "records are written as their in-memory bytes: little-endian hosts only (DESIGN.md 1)");

// ---- Kinds (DESIGN.md 3.2). 0 is never written. ----
namespace kind {
inline constexpr std::uint8_t ADD = 1;
inline constexpr std::uint8_t MODIFY = 2;
inline constexpr std::uint8_t DELETE = 3;
inline constexpr std::uint8_t CLEAR_SIDE = 4;
inline constexpr std::uint8_t CLEAR_BOOK = 5;
inline constexpr std::uint8_t CLEAR_CHANNEL = 6;
inline constexpr std::uint8_t EXEC_SUMMARY = 7;
inline constexpr std::uint8_t TRADE = 8;
inline constexpr std::uint8_t TRADE_BUST = 9;
inline constexpr std::uint8_t SESSION = 10;
inline constexpr std::uint8_t SEQ_RESET = 11;
inline constexpr std::uint8_t END_OF_EVENT = 12;
}  // namespace kind

// ---- Flags, byte 9 (DESIGN.md 3.3) ----
namespace flag {
inline constexpr std::uint8_t END_OF_EVENT = 0x01;   // matchEventIndicator bit 7
inline constexpr std::uint8_t RECOVERY = 0x02;       // bit 5, RecoveryMsg
inline constexpr std::uint8_t IMPLIED = 0x04;        // bit 4
inline constexpr std::uint8_t BOOK_NOT_CLEAN = 0x08; // its table instrument is not CLEAN (today: STALE)
}  // namespace flag

// ---- Side, byte 10 ----
namespace side {
inline constexpr std::uint8_t NONE = 0;
inline constexpr std::uint8_t BID = 1;
inline constexpr std::uint8_t OFFER = 2;
}  // namespace side

// ---- Port 1 record, 64 bytes (DESIGN.md 3.1) ----
struct EventRecord {
    std::uint32_t seq_num = 0;
    std::uint16_t seq_version = 0;
    std::uint8_t channel = 0;
    std::uint8_t msg_index = 0;
    std::uint8_t kind = 0;
    std::uint8_t flags = 0;
    std::uint8_t side = 0;
    std::uint8_t venue_msg_type = 0;
    std::uint32_t rpt_seq = 0;
    std::uint64_t instrument_id = 0;
    std::int64_t price = 0;
    std::int64_t qty = 0;
    std::uint64_t order_id = 0;
    std::uint64_t exch_time = 0;
    std::uint64_t aux = 0;
};
static_assert(sizeof(EventRecord) == 64);
static_assert(offsetof(EventRecord, seq_num) == 0);
static_assert(offsetof(EventRecord, seq_version) == 4);
static_assert(offsetof(EventRecord, channel) == 6);
static_assert(offsetof(EventRecord, msg_index) == 7);
static_assert(offsetof(EventRecord, kind) == 8);
static_assert(offsetof(EventRecord, flags) == 9);
static_assert(offsetof(EventRecord, side) == 10);
static_assert(offsetof(EventRecord, venue_msg_type) == 11);
static_assert(offsetof(EventRecord, rpt_seq) == 12);
static_assert(offsetof(EventRecord, instrument_id) == 16);
static_assert(offsetof(EventRecord, price) == 24);
static_assert(offsetof(EventRecord, qty) == 32);
static_assert(offsetof(EventRecord, order_id) == 40);
static_assert(offsetof(EventRecord, exch_time) == 48);
static_assert(offsetof(EventRecord, aux) == 56);

// ---- File header, 32 bytes, both files (DESIGN.md 2) ----
inline constexpr std::uint16_t kFormatVersion = 1;
inline constexpr char kEventsMagic[8] = {'B', '3', 'P', 'O', 'R', 'T', '1', '\0'};
inline constexpr char kBookMagic[8] = {'B', '3', 'P', 'O', 'R', 'T', '2', '\0'};

struct FileHeader {
    char magic[8] = {};
    std::uint16_t format_version = kFormatVersion;
    std::uint16_t header_size = 32;
    std::uint32_t record_size = 0;
    std::uint16_t published_depth = 0;   // N
    std::uint16_t tracked_depth = 0;     // K, 0 = uncapped
    std::uint32_t max_instruments = 0;
    std::uint64_t reserved = 0;
};
static_assert(sizeof(FileHeader) == 32);
static_assert(offsetof(FileHeader, magic) == 0);
static_assert(offsetof(FileHeader, format_version) == 8);
static_assert(offsetof(FileHeader, header_size) == 10);
static_assert(offsetof(FileHeader, record_size) == 12);
static_assert(offsetof(FileHeader, published_depth) == 16);
static_assert(offsetof(FileHeader, tracked_depth) == 18);
static_assert(offsetof(FileHeader, max_instruments) == 20);
static_assert(offsetof(FileHeader, reserved) == 24);

// The events.bin header of this build (book parameters from book_params.hpp).
inline FileHeader events_header() noexcept {
    FileHeader h;
    for (std::size_t i = 0; i < sizeof h.magic; ++i) h.magic[i] = kEventsMagic[i];
    h.record_size = sizeof(EventRecord);
    h.published_depth = static_cast<std::uint16_t>(kPublishedDepth);
    h.tracked_depth = kBookMode == BookMode::Uncapped ? 0 : static_cast<std::uint16_t>(kTrackedDepth);
    h.max_instruments = static_cast<std::uint32_t>(kMaxInstruments);
    return h;
}

// ---- Port 2: book records (DESIGN.md 4.1) ----
namespace record_type {
inline constexpr std::uint8_t BOOK_UPDATE = 1;
inline constexpr std::uint8_t STATE_CHANGE = 2;
inline constexpr std::uint8_t STATUS = 3;
}  // namespace record_type

namespace book_state {
inline constexpr std::uint8_t CLEAN = 0;
inline constexpr std::uint8_t HOLDING = 1;      // reserved, step 11
inline constexpr std::uint8_t STALE = 2;
inline constexpr std::uint8_t RECOVERING = 3;   // reserved, step 12
}  // namespace book_state

namespace book_flag {
inline constexpr std::uint8_t RECOVERY = 0x01;
inline constexpr std::uint8_t DEPTH_INCOMPLETE_BID = 0x02;
inline constexpr std::uint8_t DEPTH_INCOMPLETE_OFFER = 0x04;
}  // namespace book_flag

// What caused a STATE_CHANGE (header offset 45; 0 on other record types).
namespace cause {
inline constexpr std::uint8_t GAP = 1;
inline constexpr std::uint8_t DISCARDED_TAIL = 2;
inline constexpr std::uint8_t RPT_SEQ_JUMP = 3;
inline constexpr std::uint8_t SILENCE = 4;
inline constexpr std::uint8_t EMPTY_BOOK = 5;
inline constexpr std::uint8_t CHANNEL_RESET = 6;
}  // namespace cause

struct BookLevel {
    std::int64_t price = 0;
    std::int64_t qty = 0;
    std::uint32_t order_count = 0;
    std::uint32_t reserved = 0;
};
static_assert(sizeof(BookLevel) == 24);
static_assert(offsetof(BookLevel, qty) == 8);
static_assert(offsetof(BookLevel, order_count) == 16);

struct BookHeader {
    std::uint64_t out_seq = 0;
    std::uint32_t seq_num = 0;
    std::uint16_t seq_version = 0;
    std::uint8_t channel = 0;
    std::uint8_t msg_index = 0;
    std::uint64_t instrument_id = 0;
    std::uint64_t exch_time = 0;      // on STATUS: ticks_skipped
    std::uint64_t capture_time = 0;
    std::uint8_t record_type = 0;
    std::uint8_t state = 0;
    std::uint8_t flags = 0;
    std::uint8_t bid_count = 0;
    std::uint8_t offer_count = 0;
    std::uint8_t cause = 0;           // STATE_CHANGE only
    std::uint8_t reserved[2] = {};
};
static_assert(sizeof(BookHeader) == 48);
static_assert(offsetof(BookHeader, out_seq) == 0);
static_assert(offsetof(BookHeader, seq_num) == 8);
static_assert(offsetof(BookHeader, seq_version) == 12);
static_assert(offsetof(BookHeader, channel) == 14);
static_assert(offsetof(BookHeader, msg_index) == 15);
static_assert(offsetof(BookHeader, instrument_id) == 16);
static_assert(offsetof(BookHeader, exch_time) == 24);
static_assert(offsetof(BookHeader, capture_time) == 32);
static_assert(offsetof(BookHeader, record_type) == 40);
static_assert(offsetof(BookHeader, state) == 41);
static_assert(offsetof(BookHeader, flags) == 42);
static_assert(offsetof(BookHeader, bid_count) == 43);
static_assert(offsetof(BookHeader, offer_count) == 44);
static_assert(offsetof(BookHeader, cause) == 45);

// Record size = 48 + 2 x N x 24, rounded up to a multiple of 64 (DESIGN.md 4.1).
constexpr std::size_t book_record_size(std::size_t n) noexcept {
    return (sizeof(BookHeader) + 2 * n * sizeof(BookLevel) + 63) / 64 * 64;
}
static_assert(book_record_size(5) == 320);

template <std::size_t N>
struct BookRecordT {
    BookHeader header;
    BookLevel bids[N];
    BookLevel offers[N];
    std::array<std::uint8_t, book_record_size(N) - sizeof(BookHeader) - 2 * N * sizeof(BookLevel)> pad{};
};
using BookRecord = BookRecordT<kPublishedDepth>;
static_assert(sizeof(BookRecord) == book_record_size(kPublishedDepth));
static_assert(offsetof(BookRecord, bids) == 48);

// The book.bin header of this build, with the STATUS interval at offset 24.
inline FileHeader book_header(std::uint64_t status_interval_ns) noexcept {
    FileHeader h;
    for (std::size_t i = 0; i < sizeof h.magic; ++i) h.magic[i] = kBookMagic[i];
    h.record_size = sizeof(BookRecord);
    h.published_depth = static_cast<std::uint16_t>(kPublishedDepth);
    h.tracked_depth = kBookMode == BookMode::Uncapped ? 0 : static_cast<std::uint16_t>(kTrackedDepth);
    h.max_instruments = static_cast<std::uint32_t>(kMaxInstruments);
    h.reserved = status_interval_ns;
    return h;
}

}  // namespace b3::records
