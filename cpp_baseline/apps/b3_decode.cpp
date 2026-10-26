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
//
// What it must not do:
//   Decode fields (step 5), apply sequencing (step 6), or do I/O or
//   allocate inside the packet loop.
//
// Usage: b3_decode [--list FILE] FILE.pcap

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

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

// Counts every message; when listing is on, also records it.
struct DecodeSink {
    b3::walker::CountingSink counts;
    std::vector<ListEntry>* list = nullptr;   // reserved before the loop

    void record(const b3::walker::MessageView& m) noexcept {
        if (list != nullptr) {
            list->push_back({m.packet.seq_num, m.packet.seq_version, m.msg_index, m.template_id,
                             m.message_length, m.packet.channel});
        }
    }
    template <std::uint16_t TemplateId>
    void on(const b3::walker::MessageView& m) noexcept {
        counts.on<TemplateId>(m);
        record(m);
    }
    void on_unknown(const b3::walker::MessageView& m) noexcept {
        counts.on_unknown(m);
        record(m);
    }
};

using ull = unsigned long long;

}  // namespace

int main(int argc, char** argv) {
    std::string path;
    std::string list_path;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--list") == 0 && i + 1 < argc) {
            list_path = argv[++i];
        } else if (path.empty() && argv[i][0] != '-') {
            path = argv[i];
        } else {
            path.clear();
            break;
        }
    }
    if (path.empty()) {
        std::fprintf(stderr, "usage: b3_decode [--list FILE] FILE.pcap\n");
        return 2;
    }

    try {
        const auto capture = b3::pcap::Capture::from_file(path);

        // ---- Setup: everything allocated before the loop ----
        auto sink = std::make_unique<DecodeSink>();
        std::vector<ListEntry> list;
        if (!list_path.empty()) {
            // At most one message per 12 payload bytes.
            list.reserve(capture.counters().payload_bytes / b3::layout::message_header::size + 1);
            sink->list = &list;
        }
        b3::packet::PacketDecoder<> decoder;
        b3::walker::Walker walker;

        // ---- The loop ----
        b3::packet::PacketHeader header;
        for (const auto& p : capture.packets()) {
            if (decoder.decode(p.payload, p.payload_len, header)) {
                walker.walk(header, p.payload, p.payload_len, *sink);
            }
        }

        // ---- Output ----
        const auto& pc = decoder.counters();
        const auto& wc = walker.counters();
        std::printf("{\"file\":\"%s\",\"datagrams\":%llu,\"short_datagrams\":%llu,\"packets\":%llu,"
                    "\"oversize_packets\":%llu,\"empty_packets\":%llu,\"messages\":%llu,"
                    "\"unknown_templates\":%llu,\"malformed_packets\":%llu,\"malformed\":{",
                    path.c_str(), static_cast<ull>(pc.datagrams), static_cast<ull>(pc.short_datagrams),
                    static_cast<ull>(pc.packets), static_cast<ull>(pc.oversize_packets),
                    static_cast<ull>(wc.empty_packets), static_cast<ull>(wc.messages),
                    static_cast<ull>(wc.unknown_templates), static_cast<ull>(wc.malformed_packets));
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
        std::printf("}}\n");

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
    }
    return 0;
}
