// b3_decode: pcap in, decode counts out (first version, step 4).
//
// What it does:
//   Loads a pcap (b3::pcap::Capture), runs every UDP payload through the
//   packet decoder and the message walker, and prints one JSON line of
//   counts: datagrams, short datagrams, packets, oversize and empty packets,
//   messages per templateId (ids below 256; larger ones are only in the
//   unknown count), unknown templates, malformed packets by reason.
//   --list FILE also writes one line per dispatched message,
//     channel seq_version seq_num msg_index templateId messageLength
//   for tools/decode_crosscheck.py. The listing goes to a buffer reserved
//   before the loop and is written to disk after it.
//   --decode FILE runs the full decode writer (include/b3/decode_writer.hpp):
//   every field of every message as JSON Lines in the shape of intent.jsonl,
//   for tools/compare_decode.py. It adds body fault counts to the JSON line.
//
// What it must not do:
//   Apply sequencing (step 6). Without --decode, do I/O or allocate inside
//   the packet loop; the decode writer is a debug path that does both.
//
//   --instruments FILE loads the instrument table (DESIGN.md 4.4) at startup.
//   Packets go through the sequencer (DESIGN.md 3.6.3); duplicates are not
//   dispatched. The tracker (DESIGN.md 3.6.4) follows rptSeq and stale flags
//   for table instruments.
//   --events FILE writes port 1 (DESIGN.md 3): the file header, then one
//   64-byte record per hot-path message and END_OF_EVENT marker, from a
//   buffer sized before the loop by a counting pass over the capture.
//   --book FILE writes port 2 (DESIGN.md 4): the file header, then book
//   records computed from port 1 and the tracker's state changes, with the
//   uncapped book (book_uncapped.hpp; the capped book is step 8b, so
//   --book needs the B3_BOOK=uncapped build). --book-levels N (default 1024)
//   is its capacity per side; --status-interval-ns N (default 100 ms) the
//   STATUS interval. --silence-ns N (default 3 s; 0 off) is the silence
//   rule's limit (DESIGN.md 3.6.4).
//   --checkpoints FILE --checkpoint-out FILE: after each message whose
//   placement ("channel version seq index" per line) is listed, every table
//   instrument's state and top N levels, one line each, for the book-key
//   check (tools/book_compare.py). Debug only.
//   --counters FILE writes every counter as one JSON object, with each
//   instrument's first stale point and the build parameters, for
//   tools/counters_compare.py.
//
// Usage: b3_decode [--instruments FILE] [--events FILE] [--book FILE] [--book-levels N]
//                  [--status-interval-ns N] [--checkpoints FILE --checkpoint-out FILE]
//                  [--counters FILE] [--list FILE] [--decode FILE] FILE.pcap

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <unordered_set>
#include <string>
#include <vector>

#include "b3/decode_writer.hpp"
#include "b3/event_sink.hpp"
#include "b3/records.hpp"
#include "b3/book_params.hpp"
#include "b3/book_sink.hpp"
#include "b3/book_uncapped.hpp"
#include "b3/instruments.hpp"
#include "b3/tracker.hpp"
#include "b3/packet.hpp"
#include "b3/pcap.hpp"
#include "b3/walker.hpp"

namespace {

struct ListEntry {
    std::uint32_t seq_num;
    std::uint16_t seq_version;
    std::uint16_t msg_index;
    std::uint16_t template_id;
    std::uint16_t message_length;
    std::uint8_t channel;
};

// Counts every message; when listing is on, also records it; when decoding
// is on, also writes it in full.
struct DecodeSink {
    b3::walker::CountingSink counts;
    std::vector<ListEntry>* list = nullptr;   // reserved before the loop
    b3::decode_writer::DecodeWriter* writer = nullptr;
    b3::tracker::Tracker* tracker = nullptr;
    b3::event_sink::EventSink* events = nullptr;
    b3::book::BookSink<>* book = nullptr;
    // --checkpoints: placements to dump the books at, and the dump.
    const std::unordered_set<std::uint64_t>* checkpoints = nullptr;
    std::string* checkpoint_out = nullptr;
    const b3::instruments::Table* table = nullptr;
    const b3::book::UncappedBook* levels = nullptr;

    static std::uint64_t placement(std::uint8_t ch, std::uint16_t ver, std::uint32_t seq, std::uint8_t index) {
        return (std::uint64_t{ch} << 56) | (std::uint64_t{ver} << 40) | (std::uint64_t{seq} << 8) | index;
    }

    // After a message: port 2 from its port 1 record(s) and state changes,
    // then the checkpoint dump.
    void after(const b3::walker::MessageView& m, std::size_t records_before) {
        if (book != nullptr) {
            book->on_message(events->data() + records_before, events->size() - records_before,
                             tracker->transitions(), tracker->transition_count());
        }
        if (checkpoints != nullptr &&
            checkpoints->count(placement(m.packet.channel, m.packet.seq_version, m.packet.seq_num,
                                         static_cast<std::uint8_t>(m.msg_index)))) {
            dump(m);
        }
    }

    void dump(const b3::walker::MessageView& m) {
        b3::records::BookLevel lv[b3::kPublishedDepth];
        for (std::size_t slot = 0; slot < table->size(); ++slot) {
            *checkpoint_out += std::to_string(m.packet.channel) + " " + std::to_string(m.packet.seq_version) + " " +
                               std::to_string(m.packet.seq_num) + " " + std::to_string(m.msg_index) + " " +
                               std::to_string((*table)[slot].security_id) + " " +
                               (tracker->state(slot).stale ? "STALE" : "CLEAN");
            for (const auto side : {b3::book::Side::bid, b3::book::Side::offer}) {
                const std::size_t n = levels->top(slot, side, lv, b3::kPublishedDepth);
                *checkpoint_out += " " + std::to_string(n);
                for (std::size_t i = 0; i < n; ++i) {
                    *checkpoint_out += " " + std::to_string(lv[i].price) + " " + std::to_string(lv[i].qty) + " " +
                                       std::to_string(lv[i].order_count);
                }
            }
            *checkpoint_out += "\n";
        }
    }

    void record(const b3::walker::MessageView& m) noexcept {
        if (list != nullptr) {
            list->push_back({m.packet.seq_num, m.packet.seq_version, m.msg_index, m.template_id,
                             m.message_length, m.packet.channel});
        }
    }
    template <std::uint16_t TemplateId>
    void on(const b3::walker::MessageView& m) {
        counts.on<TemplateId>(m);
        record(m);
        tracker->on<TemplateId>(m);
        const std::size_t before = events != nullptr ? events->size() : 0;
        if (events != nullptr) events->on<TemplateId>(m);
        after(m, before);
        if (writer != nullptr) writer->on<TemplateId>(m);
    }
    void on_unknown(const b3::walker::MessageView& m) {
        counts.on_unknown(m);
        record(m);
        tracker->on_unknown(m);
        const std::size_t before = events != nullptr ? events->size() : 0;
        if (events != nullptr) events->on_unknown(m);
        after(m, before);
        if (writer != nullptr) writer->on_unknown(m);
    }
};

using ull = unsigned long long;

void counter_map(std::FILE* f, const char* key, const std::string_view* names, const std::uint64_t* values,
                 std::size_t n) {
    std::fprintf(f, "\"%s\":{", key);
    for (std::size_t i = 0; i < n; ++i) {
        std::fprintf(f, "%s\"%.*s\":%llu", i ? "," : "", static_cast<int>(names[i].size()), names[i].data(),
                     static_cast<ull>(values[i]));
    }
    std::fprintf(f, "}");
}

// Every counter of a run, as one JSON object (DESIGN.md 3.6.4).
bool write_counters(const std::string& path, const std::string& pcap, const b3::packet::PacketCounters& pc,
                    const b3::packet::SequencerCounters& sq, const b3::walker::WalkCounters& wc,
                    const b3::walker::CountingSink& counts, const b3::tracker::Tracker& tr) {
    std::FILE* f = std::fopen(path.c_str(), "w");
    if (f == nullptr) return false;
    const auto& tc = tr.counters();
    std::fprintf(f, "{\"file\":\"%s\",\"params\":{\"published_depth\":%zu,\"tracked_depth\":%zu,"
                 "\"max_instruments\":%zu,\"book\":\"%s\"},",
                 pcap.c_str(), b3::kPublishedDepth, b3::kTrackedDepth, b3::kMaxInstruments,
                 b3::kBookMode == b3::BookMode::Capped ? "capped" : "uncapped");
    std::fprintf(f, "\"datagrams\":%llu,\"short_datagrams\":%llu,\"packets\":%llu,\"oversize_packets\":%llu,"
                 "\"empty_packets\":%llu,\"duplicates\":%llu,\"messages\":%llu,\"unknown_templates\":%llu,"
                 "\"msg_index_overflow\":%llu,\"malformed_packets\":%llu,",
                 static_cast<ull>(pc.datagrams), static_cast<ull>(pc.short_datagrams), static_cast<ull>(pc.packets),
                 static_cast<ull>(pc.oversize_packets), static_cast<ull>(wc.empty_packets),
                 static_cast<ull>(sq.duplicates), static_cast<ull>(wc.messages),
                 static_cast<ull>(wc.unknown_templates), static_cast<ull>(wc.msg_index_overflow),
                 static_cast<ull>(wc.malformed_packets));
    counter_map(f, "malformed", b3::walker::kMalformedNames.data(), wc.malformed.data(), wc.malformed.size());
    std::fprintf(f, ",");
    counter_map(f, "body_faults", b3::decode::kBodyFaultNames.data(), tc.body_faults.data(), tc.body_faults.size());
    std::fprintf(f, ",\"templates\":{");
    bool first = true;
    for (std::size_t id = 0; id < counts.per_template.size(); ++id) {
        if (counts.per_template[id] == 0) continue;
        std::fprintf(f, "%s\"%zu\":%llu", first ? "" : ",", id, static_cast<ull>(counts.per_template[id]));
        first = false;
    }
    std::fprintf(f, "},\"sequencing\":{\"sequenced\":%llu,\"gaps\":%llu,\"gap_events\":%llu,"
                 "\"heartbeats\":%llu,\"heartbeat_mismatches\":%llu,\"resets\":%llu,"
                 "\"reset_mismatches\":%llu,\"version_changes\":%llu,\"seq_reset_not_first\":%llu,"
                 "\"heartbeat_without_sequence\":%llu,\"late_after_gap\":%llu},",
                 static_cast<ull>(sq.sequenced), static_cast<ull>(sq.gaps), static_cast<ull>(sq.gap_events),
                 static_cast<ull>(sq.heartbeats), static_cast<ull>(sq.heartbeat_mismatches),
                 static_cast<ull>(sq.resets), static_cast<ull>(sq.reset_mismatches),
                 static_cast<ull>(sq.version_changes), static_cast<ull>(tc.seq_reset_not_first),
                 static_cast<ull>(sq.heartbeat_without_sequence), static_cast<ull>(sq.late_after_gap));
    std::fprintf(f, "\"losses\":%llu,\"silences\":%llu,\"unknown_instrument_messages\":%llu,"
                 "\"msg_before_rebuild\":%llu,\"instruments\":{",
                 static_cast<ull>(tc.losses), static_cast<ull>(tc.silences),
                 static_cast<ull>(tc.unknown_instrument_messages), static_cast<ull>(tc.msg_before_rebuild));
    for (std::size_t slot = 0; slot < tr.table().size(); ++slot) {
        const auto& e = tr.table()[slot];
        const auto& st = tr.state(slot);
        std::fprintf(f, "%s\"%llu\":{\"slot\":%zu,\"channel\":%u,\"rpt_seq_gaps\":%llu,\"stale_events\":%llu,"
                     "\"stale_at_end\":%s,\"first_stale\":",
                     slot ? "," : "", static_cast<ull>(e.security_id), slot, e.channel,
                     static_cast<ull>(st.rpt_seq_gaps), static_cast<ull>(st.stale_events),
                     st.stale ? "true" : "false");
        if (st.ever_stale) {
            std::fprintf(f, "[%u,%u,%u]}", st.first_stale.channel, st.first_stale.version, st.first_stale.seq_num);
        } else {
            std::fprintf(f, "null}");
        }
    }
    std::fprintf(f, "}}\n");
    return std::fclose(f) == 0;
}

}  // namespace

int main(int argc, char** argv) {
    std::string path;
    std::string list_path;
    std::string decode_path;
    std::string instruments_path;
    std::string counters_path;
    std::string events_path;
    std::string book_path;
    std::string checkpoints_path;
    std::string checkpoint_out_path;
    std::size_t book_levels = 1024;
    std::uint64_t status_interval = 100'000'000;
    std::uint64_t silence_ns = b3::tracker::Tracker::kDefaultSilenceNs;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--book") == 0 && i + 1 < argc) {
            book_path = argv[++i];
            continue;
        }
        if (std::strcmp(argv[i], "--book-levels") == 0 && i + 1 < argc) {
            book_levels = std::stoul(argv[++i]);
            continue;
        }
        if (std::strcmp(argv[i], "--silence-ns") == 0 && i + 1 < argc) {
            silence_ns = std::stoull(argv[++i]);
            continue;
        }
        if (std::strcmp(argv[i], "--status-interval-ns") == 0 && i + 1 < argc) {
            status_interval = std::stoull(argv[++i]);
            continue;
        }
        if (std::strcmp(argv[i], "--checkpoints") == 0 && i + 1 < argc) {
            checkpoints_path = argv[++i];
            continue;
        }
        if (std::strcmp(argv[i], "--checkpoint-out") == 0 && i + 1 < argc) {
            checkpoint_out_path = argv[++i];
            continue;
        }
        if (std::strcmp(argv[i], "--events") == 0 && i + 1 < argc) {
            events_path = argv[++i];
            continue;
        }
        if (std::strcmp(argv[i], "--instruments") == 0 && i + 1 < argc) {
            instruments_path = argv[++i];
            continue;
        }
        if (std::strcmp(argv[i], "--counters") == 0 && i + 1 < argc) {
            counters_path = argv[++i];
            continue;
        }
        if (std::strcmp(argv[i], "--list") == 0 && i + 1 < argc) {
            list_path = argv[++i];
        } else if (std::strcmp(argv[i], "--decode") == 0 && i + 1 < argc) {
            decode_path = argv[++i];
        } else if (path.empty() && argv[i][0] != '-') {
            path = argv[i];
        } else {
            path.clear();
            break;
        }
    }
    if (path.empty()) {
        std::fprintf(stderr, "usage: b3_decode [--instruments FILE] [--events FILE] [--counters FILE] "
                             "[--list FILE] [--decode FILE] FILE.pcap\n");
        return 2;
    }

    try {
        b3::instruments::Table instruments;
        if (!instruments_path.empty()) instruments = b3::instruments::Table::load(instruments_path);
        const auto capture = b3::pcap::Capture::from_file(path);

        // ---- Setup: everything allocated before the loop ----
        auto sink = std::make_unique<DecodeSink>();
        std::vector<ListEntry> list;
        if (!list_path.empty()) {
            // At most one message per 12 payload bytes.
            list.reserve(capture.counters().payload_bytes / b3::layout::message_header::size + 1);
            sink->list = &list;
        }
        std::FILE* decode_out = nullptr;
        std::unique_ptr<b3::decode_writer::DecodeWriter> writer;
        if (!decode_path.empty()) {
            decode_out = std::fopen(decode_path.c_str(), "w");
            if (decode_out == nullptr) {
                std::fprintf(stderr, "b3_decode: cannot write %s\n", decode_path.c_str());
                return 1;
            }
            writer = std::make_unique<b3::decode_writer::DecodeWriter>(decode_out);
            sink->writer = writer.get();
        }
        // Port 1 buffer: at most one record per complete message (a record or
        // a marker, never both), counted once over the loaded capture.
        const bool want_book = !book_path.empty() || !checkpoints_path.empty();
        if (want_book && b3::kBookMode != b3::BookMode::Uncapped) {
            std::fprintf(stderr, "b3_decode: --book and --checkpoints need the uncapped build (B3_BOOK=uncapped); "
                                 "the capped book is step 8b\n");
            return 2;
        }
        std::vector<b3::records::EventRecord> event_buffer;
        std::unique_ptr<b3::event_sink::EventSink> event_sink;
        if (!events_path.empty() || want_book) {
            std::size_t capacity = 0;
            for (const auto& p : capture.packets()) {
                capacity += b3::framing::count_complete_messages(p.payload, p.payload_len);
            }
            event_buffer.resize(capacity);
        }
        b3::packet::PacketDecoder<> decoder;
        b3::walker::Walker walker;
        // Bodies (groups, var data) are walked only for the software counters.
        const bool walk_bodies = !counters_path.empty() || !decode_path.empty();
        auto tracker = std::make_unique<b3::tracker::Tracker>(instruments, decoder.sequencer(), walk_bodies,
                                                              silence_ns);
        sink->tracker = tracker.get();
        if (!events_path.empty() || want_book) {
            // After the tracker: the "book not clean" bit reads its state (DESIGN.md 3.3).
            event_sink = std::make_unique<b3::event_sink::EventSink>(event_buffer.data(), event_buffer.size(),
                                                                     tracker.get());
            sink->events = event_sink.get();
        }

        // Port 2: the uncapped book and a record buffer, both allocated here.
        // Capacity: one record per message, plus the STATUS ticks and a margin
        // for state changes; past it, records are counted, never stored.
        std::unique_ptr<b3::book::UncappedBook> levels;
        std::unique_ptr<std::uint8_t[]> book_storage;
        std::unique_ptr<b3::book::BookSink<>> book_sink;
        std::size_t book_capacity = 0;
        std::unordered_set<std::uint64_t> checkpoints;
        std::string checkpoint_out;
        if (want_book) {
            levels = std::make_unique<b3::book::UncappedBook>(b3::kMaxInstruments, book_levels);
            const auto& pk = capture.packets();
            const std::uint64_t span = pk.empty() ? 0 : pk.back().timestamp_ns - pk.front().timestamp_ns;
            book_capacity = event_buffer.size() + (status_interval ? span / status_interval + 1 : 0) +
                            4 * instruments.size() + 1024;
            book_storage.reset(new std::uint8_t[book_capacity * sizeof(b3::records::BookRecord)]);   // uninitialized
            book_sink = std::make_unique<b3::book::BookSink<>>(
                instruments, *tracker, *levels, reinterpret_cast<b3::records::BookRecord*>(book_storage.get()),
                book_capacity, status_interval);
            sink->book = book_sink.get();
            sink->table = &instruments;
            sink->levels = levels.get();
        }
        if (!checkpoints_path.empty()) {
            std::ifstream in(checkpoints_path);
            if (!in) throw b3::pcap::Error(checkpoints_path + ": cannot open");
            unsigned ch = 0, ver = 0, idx = 0;
            unsigned long seq = 0;
            while (in >> ch >> ver >> seq >> idx) {
                checkpoints.insert(DecodeSink::placement(static_cast<std::uint8_t>(ch), static_cast<std::uint16_t>(ver),
                                                         static_cast<std::uint32_t>(seq), static_cast<std::uint8_t>(idx)));
            }
            sink->checkpoints = &checkpoints;
            sink->checkpoint_out = &checkpoint_out;
        }

        // ---- The loop ----
        b3::packet::PacketHeader header;
        for (const auto& p : capture.packets()) {
            // Time advances: STATUS, then the silence rule, then the datagram.
            if (book_sink) book_sink->begin_datagram(p.timestamp_ns);
            tracker->on_time(p.timestamp_ns);
            if (book_sink) book_sink->on_transitions(tracker->transitions(), tracker->transition_count());
            if (p.payload_len >= b3::layout::packet_header::size) tracker->saw(p.payload[0], p.timestamp_ns);
            if (!decoder.decode(p.payload, p.payload_len, header)) continue;
            const auto& verdict = decoder.sequencer().last();
            if (verdict.kind == b3::packet::SequenceVerdict::Kind::gap) {
                tracker->on_loss({header.channel, verdict.gap_version, verdict.gap_first}, b3::records::cause::GAP);
                if (book_sink) book_sink->on_transitions(tracker->transitions(), tracker->transition_count());
            }
            if (walker.walk(header, p.payload, p.payload_len, *sink)) {
                tracker->on_loss({header.channel, header.seq_version, header.seq_num},
                                 b3::records::cause::DISCARDED_TAIL);
                if (book_sink) book_sink->on_transitions(tracker->transitions(), tracker->transition_count());
            }
        }

        // ---- Output ----
        const auto& pc = decoder.counters();
        const auto& wc = walker.counters();
        std::printf("{\"file\":\"%s\",\"datagrams\":%llu,\"short_datagrams\":%llu,\"packets\":%llu,"
                    "\"oversize_packets\":%llu,\"empty_packets\":%llu,\"messages\":%llu,"
                    "\"unknown_templates\":%llu,\"msg_index_overflow\":%llu,\"malformed_packets\":%llu,"
                    "\"malformed\":{",
                    path.c_str(), static_cast<ull>(pc.datagrams), static_cast<ull>(pc.short_datagrams),
                    static_cast<ull>(pc.packets), static_cast<ull>(pc.oversize_packets),
                    static_cast<ull>(wc.empty_packets), static_cast<ull>(wc.messages),
                    static_cast<ull>(wc.unknown_templates), static_cast<ull>(wc.msg_index_overflow),
                    static_cast<ull>(wc.malformed_packets));
        for (std::size_t r = 0; r < b3::walker::kMalformedReasons; ++r) {
            std::printf("%s\"%.*s\":%llu", r ? "," : "",
                        static_cast<int>(b3::walker::kMalformedNames[r].size()),
                        b3::walker::kMalformedNames[r].data(), static_cast<ull>(wc.malformed[r]));
        }
        std::printf("},\"templates\":{");
        bool first = true;
        for (std::size_t id = 0; id < sink->counts.per_template.size(); ++id) {
            if (sink->counts.per_template[id] != 0) {
                std::printf("%s\"%zu\":%llu", first ? "" : ",", id,
                            static_cast<ull>(sink->counts.per_template[id]));
                first = false;
            }
        }
        std::printf("}");
        const auto& sq = decoder.sequencer().counters();
        std::printf(",\"rejected_packets\":%llu,\"instruments\":%zu,\"sequencing\":{\"sequenced\":%llu,"
                    "\"gaps\":%llu,\"gap_events\":%llu,\"duplicates\":%llu,\"heartbeats\":%llu,"
                    "\"heartbeat_mismatches\":%llu,\"resets\":%llu,\"reset_mismatches\":%llu,"
                    "\"version_changes\":%llu}",
                    static_cast<ull>(pc.rejected_packets), instruments.size(),
                    static_cast<ull>(sq.sequenced), static_cast<ull>(sq.gaps),
                    static_cast<ull>(sq.gap_events), static_cast<ull>(sq.duplicates),
                    static_cast<ull>(sq.heartbeats), static_cast<ull>(sq.heartbeat_mismatches),
                    static_cast<ull>(sq.resets), static_cast<ull>(sq.reset_mismatches),
                    static_cast<ull>(sq.version_changes));
        if (writer) {
            std::fclose(decode_out);
            std::printf(",\"decoded_rows\":%llu,\"body_faults\":{", static_cast<ull>(writer->rows()));
            for (std::size_t r = 0; r < b3::decode::kBodyFaults; ++r) {
                std::printf("%s\"%.*s\":%llu", r ? "," : "",
                            static_cast<int>(b3::decode::kBodyFaultNames[r].size()),
                            b3::decode::kBodyFaultNames[r].data(),
                            static_cast<ull>(writer->body_faults()[r]));
            }
            std::printf("}");
        }
        if (event_sink && !events_path.empty()) {
            const auto& ec = event_sink->counters();
            std::printf(",\"events\":{\"records\":%llu,\"markers\":%llu,\"capacity\":%zu,\"overflow\":%llu,"
                        "\"resets_with_event_open\":%llu}",
                        static_cast<ull>(ec.records), static_cast<ull>(ec.markers), event_buffer.size(),
                        static_cast<ull>(ec.overflow), static_cast<ull>(ec.resets_with_event_open));
        }
        if (book_sink) {
            const auto& bc = book_sink->counters();
            const auto& lc = levels->counters();
            std::printf(",\"book\":{\"records\":%zu,\"book_updates\":%llu,\"state_changes\":%llu,\"statuses\":%llu,"
                        "\"capacity\":%zu,\"overflow\":%llu,\"untracked_records\":%llu,\"level_overflow\":%llu,"
                        "\"consistency_errors\":%llu,\"consistency_errors_stale\":%llu,\"null_price_orders\":%llu,"
                        "\"levels_per_side\":%zu,\"state_bytes\":%zu}",
                        book_sink->size(), static_cast<ull>(bc.book_updates), static_cast<ull>(bc.state_changes),
                        static_cast<ull>(bc.statuses), book_capacity, static_cast<ull>(bc.overflow),
                        static_cast<ull>(bc.untracked_records), static_cast<ull>(lc.level_overflow),
                        static_cast<ull>(lc.consistency_errors), static_cast<ull>(lc.consistency_errors_stale),
                        static_cast<ull>(lc.null_price_orders), levels->levels_per_side(), levels->state_bytes());
        }
        std::printf("}\n");

        if (book_sink && !book_path.empty()) {
            std::FILE* out = std::fopen(book_path.c_str(), "wb");
            const auto header_out = b3::records::book_header(status_interval);
            const bool ok = out != nullptr && std::fwrite(&header_out, sizeof header_out, 1, out) == 1 &&
                            std::fwrite(book_sink->data(), sizeof(b3::records::BookRecord), book_sink->size(), out) ==
                                book_sink->size();
            if (out == nullptr || std::fclose(out) != 0 || !ok) {
                std::fprintf(stderr, "b3_decode: cannot write %s\n", book_path.c_str());
                return 1;
            }
        }
        if (!checkpoint_out_path.empty()) {
            std::FILE* out = std::fopen(checkpoint_out_path.c_str(), "w");
            if (out == nullptr || std::fwrite(checkpoint_out.data(), 1, checkpoint_out.size(), out) != checkpoint_out.size() ||
                std::fclose(out) != 0) {
                std::fprintf(stderr, "b3_decode: cannot write %s\n", checkpoint_out_path.c_str());
                return 1;
            }
        }

        if (event_sink && !events_path.empty()) {
            std::FILE* out = std::fopen(events_path.c_str(), "wb");
            const auto header = b3::records::events_header();
            const bool ok = out != nullptr && std::fwrite(&header, sizeof header, 1, out) == 1 &&
                            std::fwrite(event_sink->data(), sizeof(b3::records::EventRecord), event_sink->size(),
                                        out) == event_sink->size();
            if (out != nullptr && std::fclose(out) != 0) {
                std::fprintf(stderr, "b3_decode: cannot write %s\n", events_path.c_str());
                return 1;
            }
            if (!ok) {
                std::fprintf(stderr, "b3_decode: cannot write %s\n", events_path.c_str());
                return 1;
            }
        }

        if (!counters_path.empty() &&
            !write_counters(counters_path, path, pc, sq, wc, sink->counts, *tracker)) {
            std::fprintf(stderr, "b3_decode: cannot write %s\n", counters_path.c_str());
            return 1;
        }

        if (!list_path.empty()) {
            std::FILE* out = std::fopen(list_path.c_str(), "w");
            if (out == nullptr) {
                std::fprintf(stderr, "b3_decode: cannot write %s\n", list_path.c_str());
                return 1;
            }
            for (const auto& e : list) {
                std::fprintf(out, "%u %u %u %u %u %u\n", e.channel, e.seq_version, e.seq_num,
                             e.msg_index, e.template_id, e.message_length);
            }
            std::fclose(out);
        }
    } catch (const b3::pcap::Error& e) {
        std::fprintf(stderr, "b3_decode: %s\n", e.what());
        return 1;
    } catch (const b3::instruments::LoadError& e) {
        std::fprintf(stderr, "b3_decode: %s\n", e.what());
        return 1;
    }
    return 0;
}
