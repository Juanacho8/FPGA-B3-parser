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
//   --counters FILE writes every counter as one JSON object, with each
//   instrument's first stale point and the build parameters, for
//   tools/counters_compare.py.
//
// Usage: b3_decode [--instruments FILE] [--events FILE] [--counters FILE] [--list FILE]
//                  [--decode FILE] FILE.pcap

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "b3/decode_writer.hpp"
#include "b3/event_sink.hpp"
#include "b3/records.hpp"
#include "b3/book_params.hpp"
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
        if (events != nullptr) events->on<TemplateId>(m);
        if (writer != nullptr) writer->on<TemplateId>(m);
    }
    void on_unknown(const b3::walker::MessageView& m) {
        counts.on_unknown(m);
        record(m);
        tracker->on_unknown(m);
        if (events != nullptr) events->on_unknown(m);
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
                 "\"heartbeat_without_sequence\":%llu},",
                 static_cast<ull>(sq.sequenced), static_cast<ull>(sq.gaps), static_cast<ull>(sq.gap_events),
                 static_cast<ull>(sq.heartbeats), static_cast<ull>(sq.heartbeat_mismatches),
                 static_cast<ull>(sq.resets), static_cast<ull>(sq.reset_mismatches),
                 static_cast<ull>(sq.version_changes), static_cast<ull>(tc.seq_reset_not_first),
                 static_cast<ull>(sq.heartbeat_without_sequence));
    std::fprintf(f, "\"losses\":%llu,\"unknown_instrument_messages\":%llu,\"msg_before_rebuild\":%llu,"
                 "\"instruments\":{",
                 static_cast<ull>(tc.losses), static_cast<ull>(tc.unknown_instrument_messages),
                 static_cast<ull>(tc.msg_before_rebuild));
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
    for (int i = 1; i < argc; ++i) {
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
        std::vector<b3::records::EventRecord> event_buffer;
        std::unique_ptr<b3::event_sink::EventSink> event_sink;
        if (!events_path.empty()) {
            std::size_t capacity = 0;
            for (const auto& p : capture.packets()) {
                capacity += b3::framing::count_complete_messages(p.payload, p.payload_len);
            }
            event_buffer.resize(capacity);
            event_sink = std::make_unique<b3::event_sink::EventSink>(event_buffer.data(), event_buffer.size());
            sink->events = event_sink.get();
        }
        b3::packet::PacketDecoder<> decoder;
        b3::walker::Walker walker;
        // Bodies (groups, var data) are walked only for the software counters.
        const bool walk_bodies = !counters_path.empty() || !decode_path.empty();
        auto tracker = std::make_unique<b3::tracker::Tracker>(instruments, decoder.sequencer(), walk_bodies);
        sink->tracker = tracker.get();

        // ---- The loop ----
        b3::packet::PacketHeader header;
        for (const auto& p : capture.packets()) {
            if (!decoder.decode(p.payload, p.payload_len, header)) continue;
            const auto& verdict = decoder.sequencer().last();
            if (verdict.kind == b3::packet::SequenceVerdict::Kind::gap) {
                tracker->on_loss({header.channel, verdict.gap_version, verdict.gap_first});
            }
            if (walker.walk(header, p.payload, p.payload_len, *sink)) {
                tracker->on_loss({header.channel, header.seq_version, header.seq_num});   // discarded tail
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
        if (event_sink) {
            const auto& ec = event_sink->counters();
            std::printf(",\"events\":{\"records\":%llu,\"markers\":%llu,\"capacity\":%zu,\"overflow\":%llu,"
                        "\"resets_with_event_open\":%llu}",
                        static_cast<ull>(ec.records), static_cast<ull>(ec.markers), event_buffer.size(),
                        static_cast<ull>(ec.overflow), static_cast<ull>(ec.resets_with_event_open));
        }
        std::printf("}\n");

        if (event_sink) {
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
