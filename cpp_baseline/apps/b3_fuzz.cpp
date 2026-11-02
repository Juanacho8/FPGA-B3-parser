// b3_fuzz: damaged packets through the whole pipeline, in the sanitizer build.
//
// What it does:
//   Loads a capture (make fuzz: the small set's feedA.pcap), then for each
//   iteration damages one packet and runs every packet through the decoder,
//   walker, tracker, event sink (port 1), both price-level books with their
//   port 2 sinks (uncapped and capped) and the MBO check. Mutations:
//     null_field    an audited optional field (DESIGN.md 3.10) set to its null
//     invalid_side  an audited mDEntryType set to a value that is not '0'/'1'
//     old_version   the header version lowered below an audited field's
//                   sinceVersion (its block trimmed when the field is last)
//     bit_flip      1 to 4 random bits
//     byte_set      a random byte set to 0x00, 0xff, 0x7f or 0x80
//     length_field  a messageLength or blockLength set to a random value
//     truncate      the datagram cut at a random length
//     swap          the packet swapped with the next one
//   Built in Debug, AddressSanitizer and UndefinedBehaviorSanitizer abort on
//   the first crash or undefined behaviour; the death callback prints the
//   iteration and mutation. Otherwise prints one JSON object: per mutation
//   kind, iterations, losses, unappliable messages, and the iterations with
//   consistency errors while CLEAN (either book) or MBO faults or level
//   mismatches while CLEAN, with the first few kept.
//
// What it must not do:
//   Be timed, or write any output file.
//
// Usage: b3_fuzz --instruments FILE [--iterations N] [--seed S] FILE.pcap

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <random>
#include <string>
#include <vector>

#include "b3/book_capped.hpp"
#include "b3/book_mbo.hpp"
#include "b3/book_sink.hpp"
#include "b3/book_uncapped.hpp"
#include "b3/event_sink.hpp"
#include "b3/field_audit.hpp"
#include "b3/framing.hpp"
#include "b3/instruments.hpp"
#include "b3/layouts.hpp"
#include "b3/mbo_check.hpp"
#include "b3/packet.hpp"
#include "b3/pcap.hpp"
#include "b3/tracker.hpp"
#include "b3/walker.hpp"

// The death callback exists only when a sanitizer runtime is linked (Debug).
#if defined(__SANITIZE_ADDRESS__)
#define B3_HAVE_SANITIZER_CALLBACK 1
#elif defined(__has_feature)
#if __has_feature(address_sanitizer)
#define B3_HAVE_SANITIZER_CALLBACK 1
#endif
#endif
#ifdef B3_HAVE_SANITIZER_CALLBACK
#include <sanitizer/common_interface_defs.h>
#endif

namespace {

using Bytes = std::vector<std::uint8_t>;
using ull = unsigned long long;
using Capped = b3::book::CappedBook<>;
using Uncapped = b3::book::UncappedBook;

enum Kind : int { null_field, invalid_side, old_version, bit_flip, byte_set, length_field, truncate, swap, kKinds };
constexpr const char* kKindNames[kKinds] = {"null_field", "invalid_side", "old_version", "bit_flip",
                                            "byte_set",   "length_field", "truncate",    "swap"};

char g_last[256] = "before the first iteration";
#ifdef B3_HAVE_SANITIZER_CALLBACK
void on_death() { std::fprintf(stderr, "b3_fuzz: died during %s\n", g_last); }
#endif

struct Datagram {
    Bytes payload;
    std::uint64_t time;
};

std::uint16_t rd16(const Bytes& b, std::size_t off) { return static_cast<std::uint16_t>(b[off] | (b[off + 1] << 8)); }
void wr16(Bytes& b, std::size_t off, std::uint16_t v) {
    b[off] = static_cast<std::uint8_t>(v);
    b[off + 1] = static_cast<std::uint8_t>(v >> 8);
}

// One message inside a datagram, found by framing (packet header 16 bytes,
// then messageLength, encodingType, blockLength, templateId, schemaId, version).
struct Msg {
    std::size_t off;
    std::uint16_t length, block, tid, version;
};
std::vector<Msg> messages(const Bytes& p) {
    std::vector<Msg> out;
    std::size_t off = 16;
    while (off + 12 <= p.size()) {
        const std::uint16_t len = rd16(p, off);
        if (len < 12 || off + len > p.size()) break;
        out.push_back({off, len, rd16(p, off + 4), rd16(p, off + 6), rd16(p, off + 10)});
        off += len;
    }
    return out;
}

// The pipeline b3_decode runs, with both price-level books and the MBO check.
struct Run {
    const b3::instruments::Table& table;
    b3::packet::PacketDecoder<> decoder;
    b3::walker::Walker walker;
    b3::tracker::Tracker tracker;
    std::vector<b3::records::EventRecord> events_buf;
    b3::event_sink::EventSink events;
    Uncapped ub;
    std::unique_ptr<Capped> cb;
    std::vector<b3::records::BookRecord> ubuf, cbuf;
    b3::book::BookSink<Uncapped, false> us;
    b3::book::BookSink<Capped, false> cs;
    b3::book::MboBook mbo;
    b3::book::MboCheck check;

    Run(const b3::instruments::Table& t, std::size_t records)
        : table(t), tracker(t, decoder.sequencer()), events_buf(records), events(events_buf.data(), records, &tracker),
          ub(b3::kMaxInstruments, 1024), cb(std::make_unique<Capped>()), ubuf(records * 2 + 4096),
          cbuf(records * 2 + 4096), us(t, tracker, ub, ubuf.data(), ubuf.size(), 100'000'000),
          cs(t, tracker, *cb, cbuf.data(), cbuf.size(), 100'000'000), mbo(b3::kMaxInstruments, 16384),
          check(t, tracker, mbo, ub) {}

    template <std::uint16_t Id>
    void on(const b3::walker::MessageView& m) {
        tracker.on<Id>(m);
        const std::size_t before = events.size();
        events.on<Id>(m);
        const std::size_t n = events.size() - before;
        us.on_message(events.data() + before, n, tracker.transitions(), tracker.transition_count());
        cs.on_message(events.data() + before, n, tracker.transitions(), tracker.transition_count());
        check.on_message(events.data() + before, n);
    }
    void on_unknown(const b3::walker::MessageView& m) { tracker.on_unknown(m); }

    void transitions() {
        us.on_transitions(tracker.transitions(), tracker.transition_count());
        cs.on_transitions(tracker.transitions(), tracker.transition_count());
    }

    void feed(const Datagram& d) {
        us.begin_datagram(d.time);
        cs.begin_datagram(d.time);
        tracker.on_time(d.time);
        transitions();
        const auto& p = d.payload;
        if (p.size() >= b3::layout::packet_header::size) tracker.saw(p[0], d.time);
        b3::packet::PacketHeader h;
        if (!decoder.decode(p.data(), p.size(), h)) return;
        const auto& v = decoder.sequencer().last();
        if (v.kind == b3::packet::SequenceVerdict::Kind::gap) {
            tracker.on_loss({h.channel, v.gap_version, v.gap_first}, b3::records::cause::GAP);
            transitions();
        }
        if (walker.walk(h, p.data(), p.size(), *this)) {
            tracker.on_loss({h.channel, h.seq_version, h.seq_num}, b3::records::cause::DISCARDED_TAIL);
            transitions();
        }
    }
};

struct Tally {
    std::uint64_t iterations = 0, applied = 0, losses = 0, unappliable = 0;
    std::uint64_t clean_errors = 0;   // iterations with any consistency error or MBO problem while CLEAN
    std::uint64_t uncapped_errors = 0, capped_errors = 0, mbo_faults = 0, mbo_mismatches = 0;
    std::vector<std::string> first;
};

// Applies one mutation of the kind; false if the capture offers no target.
bool mutate(Kind kind, std::vector<Datagram>& d, std::mt19937_64& rng, std::string& what) {
    auto pick = [&](std::size_t n) { return static_cast<std::size_t>(rng() % n); };
    const std::size_t i = pick(d.size());
    Bytes& p = d[i].payload;
    char buf[200];
    switch (kind) {
        case null_field:
        case invalid_side:
        case old_version: {
            // A random message of the packet that has an eligible audited field.
            for (int tries = 0; tries < 64; ++tries) {
                const std::size_t j = pick(d.size());
                Bytes& q = d[j].payload;
                const auto ms = messages(q);
                if (ms.empty()) continue;
                const Msg m = ms[pick(ms.size())];
                std::vector<std::size_t> rows;
                for (std::size_t r = 0; r < b3::field_audit::kRows; ++r) {
                    const auto& f = b3::field_audit::kRowInfo[r];
                    if (f.template_id != m.tid || f.offset + f.size > m.block) continue;
                    if ((kind == null_field && f.optional) || (kind == invalid_side && f.side) ||
                        (kind == old_version && f.since_version > 0 && f.since_version <= m.version)) {
                        rows.push_back(r);
                    }
                }
                if (rows.empty()) continue;
                const std::size_t r = rows[pick(rows.size())];
                const auto& f = b3::field_audit::kRowInfo[r];
                const std::size_t at = m.off + 12 + f.offset;
                if (kind == null_field) {
                    for (std::size_t k = 0; k < f.size; ++k) q[at + k] = static_cast<std::uint8_t>(f.null_bits >> (8 * k));
                } else if (kind == invalid_side) {
                    q[at] = static_cast<std::uint8_t>('X');
                } else {
                    wr16(q, m.off + 10, static_cast<std::uint16_t>(f.since_version - 1));
                    if (f.offset + f.size == m.block) {   // the last field: trim the block, as an old encoder would
                        q.erase(q.begin() + static_cast<std::ptrdiff_t>(at), q.begin() + static_cast<std::ptrdiff_t>(at + f.size));
                        wr16(q, m.off, static_cast<std::uint16_t>(m.length - f.size));
                        wr16(q, m.off + 4, static_cast<std::uint16_t>(m.block - f.size));
                    }
                }
                std::snprintf(buf, sizeof buf, "%s on %.*s in datagram %zu", kKindNames[kind],
                              static_cast<int>(b3::field_audit::kRowNames[r].size()), b3::field_audit::kRowNames[r].data(), j);
                what = buf;
                return true;
            }
            return false;
        }
        case bit_flip: {
            if (p.empty()) return false;
            const int n = 1 + static_cast<int>(pick(4));
            for (int k = 0; k < n; ++k) p[pick(p.size())] ^= static_cast<std::uint8_t>(1u << pick(8));
            std::snprintf(buf, sizeof buf, "bit_flip x%d in datagram %zu", n, i);
            break;
        }
        case byte_set: {
            if (p.empty()) return false;
            static constexpr std::uint8_t v[] = {0x00, 0xff, 0x7f, 0x80};
            const std::size_t at = pick(p.size());
            p[at] = v[pick(4)];
            std::snprintf(buf, sizeof buf, "byte_set at %zu in datagram %zu", at, i);
            break;
        }
        case length_field: {
            const auto ms = messages(p);
            if (ms.empty()) return false;
            const Msg m = ms[pick(ms.size())];
            const bool block = (rng() & 1) != 0;
            wr16(p, m.off + (block ? 4 : 0), static_cast<std::uint16_t>(rng()));
            std::snprintf(buf, sizeof buf, "%s in datagram %zu", block ? "blockLength" : "messageLength", i);
            break;
        }
        case truncate: {
            if (p.size() < 2) return false;
            p.resize(pick(p.size()));
            std::snprintf(buf, sizeof buf, "truncate datagram %zu to %zu bytes", i, p.size());
            break;
        }
        case swap: {
            if (i + 1 >= d.size()) return false;
            std::swap(d[i].payload, d[i + 1].payload);
            std::snprintf(buf, sizeof buf, "swap datagrams %zu and %zu", i, i + 1);
            break;
        }
        default: return false;
    }
    what = buf;
    return true;
}

std::uint64_t sum(const std::array<std::uint64_t, b3::book::kMboFaults>& a) {
    std::uint64_t n = 0;
    for (auto v : a) n += v;
    return n;
}

}  // namespace

int main(int argc, char** argv) {
    std::string path, instruments_path;
    std::uint64_t iterations = 400, seed = 1;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--instruments") == 0 && i + 1 < argc) instruments_path = argv[++i];
        else if (std::strcmp(argv[i], "--iterations") == 0 && i + 1 < argc) iterations = std::stoull(argv[++i]);
        else if (std::strcmp(argv[i], "--seed") == 0 && i + 1 < argc) seed = std::stoull(argv[++i]);
        else path = argv[i];
    }
    if (path.empty() || instruments_path.empty()) {
        std::fprintf(stderr, "usage: b3_fuzz --instruments FILE [--iterations N] [--seed S] FILE.pcap\n");
        return 2;
    }
#ifdef B3_HAVE_SANITIZER_CALLBACK
    __sanitizer_set_death_callback(on_death);
#endif
    const auto table = b3::instruments::Table::load(instruments_path);
    const auto capture = b3::pcap::Capture::from_file(path);
    std::vector<Datagram> base;
    for (const auto& p : capture.packets()) base.push_back({Bytes(p.payload, p.payload + p.payload_len), p.timestamp_ns});

    std::mt19937_64 rng(seed);
    std::array<Tally, kKinds> tally{};
    for (std::uint64_t it = 0; it < iterations; ++it) {
        const Kind kind = static_cast<Kind>(it % kKinds);
        Tally& t = tally[kind];
        ++t.iterations;
        std::vector<Datagram> d = base;
        std::string what;
        if (!mutate(kind, d, rng, what)) continue;
        ++t.applied;
        std::snprintf(g_last, sizeof g_last, "iteration %llu (seed %llu): %s", static_cast<ull>(it),
                      static_cast<ull>(seed), what.c_str());
        std::size_t records = 0;
        for (const auto& x : d) records += b3::framing::count_complete_messages(x.payload.data(), x.payload.size());
        auto run = std::make_unique<Run>(table, records + 1);
        for (const auto& x : d) run->feed(x);
        const auto& tc = run->tracker.counters();
        t.losses += tc.losses;
        for (auto v : tc.unappliable) t.unappliable += v;
        const std::uint64_t ue = run->ub.counters().consistency_errors, ce = run->cb->counters().consistency_errors;
        const std::uint64_t mf = sum(run->mbo.counters().faults), mm = run->check.counters().level_mismatches;
        t.uncapped_errors += ue;
        t.capped_errors += ce;
        t.mbo_faults += mf;
        t.mbo_mismatches += mm;
        if (ue + ce + mf + mm) {
            ++t.clean_errors;
            if (t.first.size() < 3) {
                t.first.push_back(what + ": uncapped " + std::to_string(ue) + ", capped " + std::to_string(ce) +
                                  ", mbo faults " + std::to_string(mf) + ", level mismatches " + std::to_string(mm));
            }
        }
    }
    std::printf("{\"file\":\"%s\",\"seed\":%llu,\"iterations\":%llu,\"crashes\":0,\"kinds\":{", path.c_str(),
                static_cast<ull>(seed), static_cast<ull>(iterations));
    for (int k = 0; k < kKinds; ++k) {
        const Tally& t = tally[k];
        std::printf("%s\"%s\":{\"iterations\":%llu,\"applied\":%llu,\"losses\":%llu,\"unappliable\":%llu,"
                    "\"clean_error_iterations\":%llu,\"uncapped_errors\":%llu,\"capped_errors\":%llu,"
                    "\"mbo_faults\":%llu,\"mbo_mismatches\":%llu,\"first\":[",
                    k ? "," : "", kKindNames[k], static_cast<ull>(t.iterations), static_cast<ull>(t.applied),
                    static_cast<ull>(t.losses), static_cast<ull>(t.unappliable), static_cast<ull>(t.clean_errors),
                    static_cast<ull>(t.uncapped_errors), static_cast<ull>(t.capped_errors),
                    static_cast<ull>(t.mbo_faults), static_cast<ull>(t.mbo_mismatches));
        for (std::size_t i = 0; i < t.first.size(); ++i) std::printf("%s\"%s\"", i ? "," : "", t.first[i].c_str());
        std::printf("]}");
    }
    std::printf("}}\n");
    return 0;
}
