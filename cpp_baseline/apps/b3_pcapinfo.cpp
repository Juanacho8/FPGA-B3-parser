// b3_pcapinfo: what the pcap reader sees in one file, as one JSON line.
//
// What it does:
//   Loads a pcap with b3::pcap::Capture and prints its counters, packets and
//   payload bytes per destination group (ip:port), and a CRC-32 over every
//   yielded datagram (timestamp_ns as 8 bytes LE, payload length as 4 bytes
//   LE, then the payload), in file order. tools/pcap_crosscheck.py computes
//   the same line with an independent Python reader and compares.
//   --timing adds the time to read the file and to build the index.
//
// What it must not do:
//   Interpret the payload: the CRC treats it as opaque bytes.
//
// Usage: b3_pcapinfo [--timing] FILE.pcap

#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include "b3/pcap.hpp"

namespace {

// CRC-32 (IEEE 802.3, reflected, as zlib.crc32), table driven.
class Crc32 {
public:
    Crc32() {
        for (std::uint32_t i = 0; i < 256; ++i) {
            std::uint32_t c = i;
            for (int k = 0; k < 8; ++k) {
                c = (c & 1u) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            }
            table_[i] = c;
        }
    }
    void update(const std::uint8_t* p, std::size_t n) {
        for (std::size_t i = 0; i < n; ++i) {
            crc_ = table_[(crc_ ^ p[i]) & 0xFFu] ^ (crc_ >> 8);
        }
    }
    void update_le(std::uint64_t v, int bytes) {
        for (int i = 0; i < bytes; ++i) {
            const auto b = static_cast<std::uint8_t>(v >> (8 * i));
            update(&b, 1);
        }
    }
    std::uint32_t value() const { return crc_ ^ 0xFFFFFFFFu; }

private:
    std::array<std::uint32_t, 256> table_{};
    std::uint32_t crc_ = 0xFFFFFFFFu;
};

std::string json_escape(const std::string& s) {
    std::string out;
    for (char c : s) {
        if (c == '"' || c == '\\') {
            out += '\\';
        }
        out += c;
    }
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    bool timing = false;
    std::string path;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--timing") == 0) {
            timing = true;
        } else if (path.empty()) {
            path = argv[i];
        } else {
            path.clear();
            break;
        }
    }
    if (path.empty()) {
        std::fprintf(stderr, "usage: b3_pcapinfo [--timing] FILE.pcap\n");
        return 2;
    }

    using clock = std::chrono::steady_clock;
    try {
        // Read and index separately, so --timing can report both.
        const auto t0 = clock::now();
        std::ifstream in(path, std::ios::binary | std::ios::ate);
        if (!in) {
            throw b3::pcap::Error(path + ": cannot open");
        }
        std::vector<std::uint8_t> bytes(static_cast<std::size_t>(in.tellg()));
        in.seekg(0);
        in.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
        const auto t1 = clock::now();
        const auto capture = b3::pcap::Capture::from_bytes(std::move(bytes));
        const auto t2 = clock::now();

        std::map<std::pair<std::uint32_t, std::uint16_t>, std::pair<std::uint64_t, std::uint64_t>> groups;
        Crc32 crc;
        for (const auto& p : capture.packets()) {
            auto& g = groups[{p.dst_ip, p.dst_port}];
            g.first += 1;
            g.second += p.payload_len;
            crc.update_le(p.timestamp_ns, 8);
            crc.update_le(p.payload_len, 4);
            crc.update(p.payload, p.payload_len);
        }

        const auto& c = capture.counters();
        std::printf("{\"file\":\"%s\",\"frames\":%llu,\"udp_payloads\":%llu,\"payload_bytes\":%llu,"
                    "\"non_ipv4\":%llu,\"non_udp\":%llu,\"fragments\":%llu,\"truncated\":%llu,"
                    "\"bad_lengths\":%llu,\"trailing_garbage\":%llu,\"crc32\":%u,\"groups\":{",
                    json_escape(path).c_str(),
                    static_cast<unsigned long long>(c.frames),
                    static_cast<unsigned long long>(c.udp_payloads),
                    static_cast<unsigned long long>(c.payload_bytes),
                    static_cast<unsigned long long>(c.non_ipv4),
                    static_cast<unsigned long long>(c.non_udp),
                    static_cast<unsigned long long>(c.fragments),
                    static_cast<unsigned long long>(c.truncated),
                    static_cast<unsigned long long>(c.bad_lengths),
                    static_cast<unsigned long long>(c.trailing_garbage),
                    crc.value());
        bool first = true;
        for (const auto& [key, val] : groups) {
            std::printf("%s\"%s:%u\":{\"packets\":%llu,\"bytes\":%llu}", first ? "" : ",",
                        b3::pcap::ipv4_to_string(key.first).c_str(), key.second,
                        static_cast<unsigned long long>(val.first),
                        static_cast<unsigned long long>(val.second));
            first = false;
        }
        std::printf("}");
        if (timing) {
            const auto ms = [](auto d) { return std::chrono::duration<double, std::milli>(d).count(); };
            std::printf(",\"timing\":{\"read_ms\":%.3f,\"index_ms\":%.3f,\"file_bytes\":%zu}",
                        ms(t1 - t0), ms(t2 - t1), capture.buffer().size());
        }
        std::printf("}\n");
    } catch (const b3::pcap::Error& e) {
        std::fprintf(stderr, "b3_pcapinfo: %s\n", e.what());
        return 1;
    }
    return 0;
}
