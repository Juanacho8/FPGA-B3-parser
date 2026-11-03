// b3_bench: the benchmark harness (brief section 8, step 10).
//
// What it does:
//   1. Loads and indexes the whole pcap before any timing, as b3_decode does,
//      and preallocates every output buffer.
//   2. Correctness gate: a warm-up pass writes port 1 and port 2 into the
//      buffers; their bytes must equal the goldens given (--golden-events,
//      --golden-book) for this build's parameters. Port 1: the records
//      byte for byte, and the file header with this build's N and K. Port 2:
//      the whole file. On any difference it prints the first one and reports
//      no timings (exit 1).
//   3. Timed passes (--passes, default 30), each on fresh state. Per packet,
//      from the moment its payload is handed to the packet decoder:
//        port 1 latency: to the last port 1 record written for that packet
//        port 2 latency: to the last port 2 record written because of it
//                        (only packets that produce port 2 output)
//      Timestamps are read in the pipeline only when a record was just
//      written. The clock is the system counter (mach_absolute_time on macOS,
//      cntvct_el0 elsewhere on arm64); its tick is reported, and a
//      percentile within 2 ticks is marked. Each pass is also timed whole and
//      in batches of packets, divided, for averages with no tick error.
//      Throughput: messages and packets per second over the whole file.
//   4. Reports per latency p50, p90, p99, p99.9 and max (the median over
//      passes) and their spread over passes (min to max), as one JSON file
//      and a short markdown table (--out PREFIX: PREFIX.json, PREFIX.md),
//      with the environment: CPU, cores, OS, compiler and flags, build type
//      and parameters, clock tick, power state. On battery or in low power
//      mode it refuses to run unless --allow-battery, and then marks the
//      results invalid.
//
// What it must not do:
//   Report a timing for a build whose outputs differ from the goldens, or
//   do I/O, allocate or print inside a timed pass.
//
// Usage: b3_bench --instruments FILE --golden-events FILE --golden-book FILE
//                 [--passes N] [--batch N] [--status-interval-ns N] [--allow-battery]
//                 [--out PREFIX] FILE.pcap

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>
#include <sys/utsname.h>
#include <type_traits>
#include <vector>

#if defined(__APPLE__)
#include <mach/mach_time.h>
#include <sys/sysctl.h>
#endif

#include "b3/book_params.hpp"
#include "b3/book_sink.hpp"
#include "b3/event_sink.hpp"
#include "b3/framing.hpp"
#include "b3/instruments.hpp"
#include "b3/layouts.hpp"
#include "b3/packet.hpp"
#include "b3/pcap.hpp"
#include "b3/records.hpp"
#include "b3/tracker.hpp"
#include "b3/walker.hpp"

#ifndef B3_BUILD_FLAGS
#define B3_BUILD_FLAGS "unknown"
#endif
#ifndef B3_BUILD_TYPE
#define B3_BUILD_TYPE "unknown"
#endif

namespace {

using ull = unsigned long long;
using Book = b3::book::BuildBook;
using Sink = b3::book::BookSink<>;

// ---- The clock ----
inline std::uint64_t ticks() noexcept {
#if defined(__APPLE__)
    return mach_absolute_time();   // reads the arm64 system counter (cntvct_el0)
#elif defined(__aarch64__)
    std::uint64_t v;
    asm volatile("isb; mrs %0, cntvct_el0" : "=r"(v));
    return v;
#else
#error "b3_bench needs mach_absolute_time or cntvct_el0"
#endif
}

double tick_ns() {
#if defined(__APPLE__)
    mach_timebase_info_data_t tb;
    mach_timebase_info(&tb);
    return static_cast<double>(tb.numer) / tb.denom;
#else
    std::uint64_t f;
    asm volatile("mrs %0, cntfrq_el0" : "=r"(f));
    return 1e9 / static_cast<double>(f);
#endif
}

// ---- The pipeline, as b3_decode runs it, with the latency stamps ----
constexpr std::uint64_t kNone = ~0ull;

struct Pipeline {
    b3::packet::PacketDecoder<> decoder;
    b3::walker::Walker walker;
    b3::tracker::Tracker tracker;
    b3::event_sink::EventSink events;
    std::unique_ptr<Book> book;
    Sink sink;
    std::uint64_t last1 = kNone;   // this packet's last port 1 record, ticks
    std::uint64_t last2 = kNone;   // this packet's last port 2 record

    Pipeline(const b3::instruments::Table& table, b3::records::EventRecord* ev, std::size_t ev_cap,
             b3::records::BookRecord* bk, std::size_t bk_cap, std::uint64_t interval, std::uint64_t silence)
        : tracker(table, decoder.sequencer(), false, silence), events(ev, ev_cap, &tracker),
          book(make_book()), sink(table, tracker, *book, bk, bk_cap, interval) {}

    static std::unique_ptr<Book> make_book() {
        if constexpr (std::is_same_v<Book, b3::book::UncappedBook>) {
            return std::make_unique<Book>(b3::kMaxInstruments, 1024);
        } else {
            return std::make_unique<Book>();
        }
    }

    template <std::uint16_t Id>
    void on(const b3::walker::MessageView& m) noexcept {
        tracker.on<Id>(m);
        const std::size_t before = events.size();
        events.on<Id>(m);
        const std::size_t n = events.size() - before;
        if (n) last1 = ticks();
        const std::size_t b = sink.size() + sink.counters().overflow;
        sink.on_message(events.data() + before, n, tracker.transitions(), tracker.transition_count());
        if (sink.size() + sink.counters().overflow != b) last2 = ticks();
    }
    void on_unknown(const b3::walker::MessageView& m) noexcept { tracker.on_unknown(m); }

    void transitions() noexcept {
        const std::size_t b = sink.size();
        sink.on_transitions(tracker.transitions(), tracker.transition_count());
        if (sink.size() != b) last2 = ticks();
    }

    // One datagram; returns the moment its payload went to the packet decoder.
    std::uint64_t feed(const b3::pcap::PacketView& p) noexcept {
        sink.begin_datagram(p.timestamp_ns);
        tracker.on_time(p.timestamp_ns);
        sink.on_transitions(tracker.transitions(), tracker.transition_count());
        if (p.payload_len >= b3::layout::packet_header::size) tracker.saw(p.payload[0], p.timestamp_ns);
        last1 = last2 = kNone;
        const std::uint64_t t0 = ticks();
        b3::packet::PacketHeader header;
        if (!decoder.decode(p.payload, p.payload_len, header)) return t0;
        const auto& verdict = decoder.sequencer().last();
        if (verdict.kind == b3::packet::SequenceVerdict::Kind::gap) {
            tracker.on_loss({header.channel, verdict.gap_version, verdict.gap_first}, b3::records::cause::GAP);
            transitions();
        }
        if (walker.walk(header, p.payload, p.payload_len, *this)) {
            tracker.on_loss({header.channel, header.seq_version, header.seq_num}, b3::records::cause::DISCARDED_TAIL);
            transitions();
        }
        return t0;
    }
};

// ---- Statistics ----
struct Pct {
    double p50 = 0, p90 = 0, p99 = 0, p999 = 0, max = 0;
};
Pct percentiles(std::vector<double> v) {
    Pct p;
    if (v.empty()) return p;
    std::sort(v.begin(), v.end());
    auto at = [&](double q) { return v[std::min(v.size() - 1, static_cast<std::size_t>(q * static_cast<double>(v.size())))]; };
    p.p50 = at(0.50);
    p.p90 = at(0.90);
    p.p99 = at(0.99);
    p.p999 = at(0.999);
    p.max = v.back();
    return p;
}
double median(std::vector<double> v) {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    return v[v.size() / 2];
}

// ---- Environment ----
std::string sh(const char* cmd) {
    std::string out;
    if (std::FILE* f = popen(cmd, "r")) {
        char buf[512];
        while (std::fgets(buf, sizeof buf, f)) out += buf;
        pclose(f);
    }
    while (!out.empty() && (out.back() == '\n' || out.back() == ' ')) out.pop_back();
    return out;
}
std::string json_escape(const std::string& s) {
    std::string o;
    for (char c : s) {
        if (c == '"' || c == '\\') o += '\\';
        if (c == '\n') {
            o += "\\n";
            continue;
        }
        o += c;
    }
    return o;
}

struct Env {
    std::string cpu, os, power;
    int cores = 0, perf_cores = 0;
    bool on_battery = false, low_power = false;
};
Env environment() {
    Env e;
#if defined(__APPLE__)
    char buf[256];
    std::size_t len = sizeof buf;
    if (sysctlbyname("machdep.cpu.brand_string", buf, &len, nullptr, 0) == 0) e.cpu = buf;
    int n = 0;
    len = sizeof n;
    if (sysctlbyname("hw.ncpu", &n, &len, nullptr, 0) == 0) e.cores = n;
    len = sizeof n;
    if (sysctlbyname("hw.perflevel0.physicalcpu", &n, &len, nullptr, 0) == 0) e.perf_cores = n;
    e.os = "macOS " + sh("sw_vers -productVersion") + " (" + sh("sw_vers -buildVersion") + ")";
    e.power = sh("pmset -g batt | head -1");
    e.on_battery = e.power.find("Battery Power") != std::string::npos;
    e.low_power = sh("pmset -g | awk '/lowpowermode/ {print $2}'") == "1";
#else
    utsname u{};
    uname(&u);
    e.os = std::string(u.sysname) + " " + u.release;
    e.power = "unknown";
#endif
    return e;
}

bool read_file(const std::string& path, std::vector<std::uint8_t>& out) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    out.assign(std::istreambuf_iterator<char>(in), {});
    return true;
}

std::string first_difference(const std::vector<std::uint8_t>& want, const std::vector<std::uint8_t>& got,
                             std::size_t header, std::size_t record) {
    const std::size_t n = std::min(want.size(), got.size());
    for (std::size_t i = 0; i < n; ++i) {
        if (want[i] != got[i]) {
            if (i < header) return "file header byte " + std::to_string(i);
            return "record " + std::to_string((i - header) / record) + " byte " + std::to_string((i - header) % record);
        }
    }
    return std::to_string((want.size() - header) / record) + " records expected, " +
           std::to_string((got.size() - header) / record) + " written";
}

}  // namespace

int main(int argc, char** argv) {
    std::string path, instruments_path, golden_events, golden_book, out_prefix;
    int passes = 30;
    std::size_t batch = 1024;
    std::uint64_t interval = 100'000'000;
    bool allow_battery = false;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--instruments" && i + 1 < argc) instruments_path = argv[++i];
        else if (a == "--golden-events" && i + 1 < argc) golden_events = argv[++i];
        else if (a == "--golden-book" && i + 1 < argc) golden_book = argv[++i];
        else if (a == "--passes" && i + 1 < argc) passes = std::stoi(argv[++i]);
        else if (a == "--batch" && i + 1 < argc) batch = std::stoul(argv[++i]);
        else if (a == "--status-interval-ns" && i + 1 < argc) interval = std::stoull(argv[++i]);
        else if (a == "--out" && i + 1 < argc) out_prefix = argv[++i];
        else if (a == "--allow-battery") allow_battery = true;
        else path = a;
    }
    if (path.empty() || instruments_path.empty() || golden_events.empty() || golden_book.empty() || passes < 1) {
        std::fprintf(stderr, "usage: b3_bench --instruments FILE --golden-events FILE --golden-book FILE "
                             "[--passes N] [--batch N] [--allow-battery] [--out PREFIX] FILE.pcap\n");
        return 2;
    }
    const Env env = environment();
    const bool invalid = env.on_battery || env.low_power;
    if (invalid && !allow_battery) {
        std::fprintf(stderr, "b3_bench: on battery or in low power mode (%s); refusing to time. "
                             "Plug in, or pass --allow-battery (results marked invalid).\n", env.power.c_str());
        return 3;
    }

    // ---- Load and index, preallocate (untimed) ----
    const auto table = b3::instruments::Table::load(instruments_path);
    const std::uint64_t load0 = ticks();
    const auto capture = b3::pcap::Capture::from_file(path);   // read the file and index it
    const std::uint64_t load1 = ticks();
    const auto& pk = capture.packets();
    std::size_t messages = 0;
    for (const auto& p : pk) messages += b3::framing::count_complete_messages(p.payload, p.payload_len);
    std::vector<b3::records::EventRecord> ev(messages + 1);
    const std::uint64_t span = pk.empty() ? 0 : pk.back().timestamp_ns - pk.front().timestamp_ns;
    const std::size_t bk_cap = ev.size() + (interval ? span / interval + 1 : 0) + 4 * table.size() + 1024;
    std::unique_ptr<b3::records::BookRecord[]> bk(new b3::records::BookRecord[bk_cap]);
    std::vector<std::uint64_t> t0(pk.size()), l1(pk.size()), l2(pk.size());
    const std::uint64_t silence = b3::tracker::Tracker::kDefaultSilenceNs;

    auto run = [&](std::vector<std::uint64_t>* batches) {
        auto pipe = std::make_unique<Pipeline>(table, ev.data(), ev.size(), bk.get(), bk_cap, interval, silence);
        const std::uint64_t start = ticks();
        std::uint64_t batch_start = start;
        for (std::size_t i = 0; i < pk.size(); ++i) {
            t0[i] = pipe->feed(pk[i]);
            l1[i] = pipe->last1;
            l2[i] = pipe->last2;
            if (batches && (i + 1) % batch == 0) {
                const std::uint64_t now = ticks();
                batches->push_back(now - batch_start);
                batch_start = now;
            }
        }
        const std::uint64_t total = ticks() - start;
        return std::make_pair(std::move(pipe), total);
    };

    // ---- Warm-up pass and the correctness gate ----
    auto [warm, warm_ticks] = run(nullptr);
    (void)warm_ticks;
    std::vector<std::uint8_t> want_ev, want_bk;
    if (!read_file(golden_events, want_ev) || !read_file(golden_book, want_bk)) {
        std::fprintf(stderr, "b3_bench: cannot read the goldens\n");
        return 2;
    }
    const auto eh = b3::records::events_header();
    std::vector<std::uint8_t> got_ev(reinterpret_cast<const std::uint8_t*>(&eh),
                                     reinterpret_cast<const std::uint8_t*>(&eh) + sizeof eh);
    got_ev.insert(got_ev.end(), reinterpret_cast<const std::uint8_t*>(warm->events.data()),
                  reinterpret_cast<const std::uint8_t*>(warm->events.data() + warm->events.size()));
    const auto bh = b3::records::book_header(interval);
    std::vector<std::uint8_t> got_bk(reinterpret_cast<const std::uint8_t*>(&bh),
                                     reinterpret_cast<const std::uint8_t*>(&bh) + sizeof bh);
    got_bk.insert(got_bk.end(), reinterpret_cast<const std::uint8_t*>(warm->sink.data()),
                  reinterpret_cast<const std::uint8_t*>(warm->sink.data() + warm->sink.size()));
    // Port 1's golden header records the golden's own N and K: this build's go in.
    if (want_ev.size() >= sizeof eh) std::memcpy(want_ev.data() + 16, got_ev.data() + 16, 4);
    bool ok = true;
    if (got_ev != want_ev) {
        std::fprintf(stderr, "b3_bench: port 1 differs from %s: %s\n", golden_events.c_str(),
                     first_difference(want_ev, got_ev, sizeof eh, sizeof(b3::records::EventRecord)).c_str());
        ok = false;
    }
    if (got_bk != want_bk) {
        std::fprintf(stderr, "b3_bench: port 2 differs from %s: %s\n", golden_book.c_str(),
                     first_difference(want_bk, got_bk, sizeof bh, sizeof(b3::records::BookRecord)).c_str());
        ok = false;
    }
    if (!ok) {
        std::fprintf(stderr, "b3_bench: no timings reported: the outputs do not match the goldens\n");
        return 1;
    }
    warm.reset();

    // ---- Timed passes ----
    const double tns = tick_ns();
    struct PassResult {
        Pct p1, p2;
        double ns_per_packet, ns_per_message, batch_ns_per_packet;
        std::size_t n1, n2;
    };
    std::vector<PassResult> res;
    std::vector<std::uint64_t> batches;
    batches.reserve(pk.size() / batch + 2);
    std::vector<double> v1, v2;
    v1.reserve(pk.size());
    v2.reserve(pk.size());
    for (int pass = 0; pass < passes; ++pass) {
        batches.clear();
        auto [pipe, total] = run(&batches);
        pipe.reset();
        v1.clear();
        v2.clear();
        for (std::size_t i = 0; i < pk.size(); ++i) {
            if (l1[i] != kNone) v1.push_back(static_cast<double>(l1[i] - t0[i]) * tns);
            if (l2[i] != kNone) v2.push_back(static_cast<double>(l2[i] - t0[i]) * tns);
        }
        std::vector<double> bm;
        for (auto b : batches) bm.push_back(static_cast<double>(b) * tns / static_cast<double>(batch));
        res.push_back({percentiles(v1), percentiles(v2), static_cast<double>(total) * tns / pk.size(),
                       static_cast<double>(total) * tns / static_cast<double>(messages), median(bm), v1.size(), v2.size()});
    }

    // ---- Report ----
    auto agg = [&](auto field) {
        std::vector<double> v;
        for (const auto& r : res) v.push_back(field(r));
        const auto [lo, hi] = std::minmax_element(v.begin(), v.end());
        return std::array<double, 3>{median(v), *lo, *hi};
    };
    struct Row {
        const char* name;
        std::array<double, 3> v;
    };
    const auto pkt = agg([](const PassResult& r) { return r.ns_per_packet; });
    const auto msg = agg([](const PassResult& r) { return r.ns_per_message; });
    const auto bat = agg([](const PassResult& r) { return r.batch_ns_per_packet; });
    const Row lat[] = {
        {"port1_p50", agg([](const PassResult& r) { return r.p1.p50; })},
        {"port1_p90", agg([](const PassResult& r) { return r.p1.p90; })},
        {"port1_p99", agg([](const PassResult& r) { return r.p1.p99; })},
        {"port1_p999", agg([](const PassResult& r) { return r.p1.p999; })},
        {"port1_max", agg([](const PassResult& r) { return r.p1.max; })},
        {"port2_p50", agg([](const PassResult& r) { return r.p2.p50; })},
        {"port2_p90", agg([](const PassResult& r) { return r.p2.p90; })},
        {"port2_p99", agg([](const PassResult& r) { return r.p2.p99; })},
        {"port2_p999", agg([](const PassResult& r) { return r.p2.p999; })},
        {"port2_max", agg([](const PassResult& r) { return r.p2.max; })},
    };
    const char* book = b3::kBookMode == b3::BookMode::Capped ? "capped" : "uncapped";
    const std::size_t k = b3::kBookMode == b3::BookMode::Capped ? b3::kTrackedDepth : 0;
    std::string j = "{\n";
    j += "  \"valid\": " + std::string(invalid ? "false" : "true") + ",\n";
    j += "  \"file\": \"" + json_escape(path) + "\",\n";
    j += "  \"packets\": " + std::to_string(pk.size()) + ", \"messages\": " + std::to_string(messages) +
         ", \"packets_with_port1\": " + std::to_string(res[0].n1) + ", \"packets_with_port2\": " +
         std::to_string(res[0].n2) + ",\n";
    j += "  \"passes\": " + std::to_string(passes) + ", \"batch\": " + std::to_string(batch) + ",\n";
    j += "  \"pcap_load_index_ms\": " + std::to_string(static_cast<double>(load1 - load0) * tns / 1e6) +
         ",   \"pcap_load_index_note\": \"once, before timing: the file read and indexed\",\n";
    j += "  \"golden_check\": \"port 1 and port 2 byte-identical to the goldens\",\n";
    j += "  \"environment\": {\"cpu\": \"" + json_escape(env.cpu) + "\", \"cores\": " + std::to_string(env.cores) +
         ", \"performance_cores\": " + std::to_string(env.perf_cores) + ", \"os\": \"" + json_escape(env.os) +
         "\", \"power\": \"" + json_escape(env.power) + "\", \"on_battery\": " + (env.on_battery ? "true" : "false") +
         ", \"low_power_mode\": " + (env.low_power ? "true" : "false") + ", \"compiler\": \"" +
         json_escape(__VERSION__) + "\", \"flags\": \"" + json_escape(B3_BUILD_FLAGS) + "\", \"build_type\": \"" +
         B3_BUILD_TYPE + "\", \"timed\": " + (b3::kTimed ? "true" : "false") + ", \"published_depth\": " +
         std::to_string(b3::kPublishedDepth) + ", \"tracked_depth\": " + std::to_string(k) + ", \"book\": \"" + book +
         "\", \"max_instruments\": " + std::to_string(b3::kMaxInstruments) + ", \"clock\": \"" +
#if defined(__APPLE__)
         "mach_absolute_time" +
#else
         "cntvct_el0" +
#endif
         std::string("\", \"tick_ns\": ") + std::to_string(tns) + "},\n";
    auto trio = [](const std::array<double, 3>& v) {
        char b[160];
        std::snprintf(b, sizeof b, "{\"median\": %.2f, \"min\": %.2f, \"max\": %.2f}", v[0], v[1], v[2]);
        return std::string(b);
    };
    j += "  \"ns_per_packet\": " + trio(pkt) + ",\n  \"ns_per_message\": " + trio(msg) +
         ",\n  \"batch_ns_per_packet\": " + trio(bat) + ",\n";
    char tp[200];
    std::snprintf(tp, sizeof tp, "  \"packets_per_s\": %.0f, \"messages_per_s\": %.0f,\n", 1e9 / pkt[0], 1e9 / msg[0]);
    j += tp;
    j += "  \"latency_ns\": {";
    for (std::size_t i = 0; i < std::size(lat); ++i) {
        j += std::string(i ? ", " : "") + "\"" + lat[i].name + "\": " + trio(lat[i].v);
    }
    j += "},\n  \"near_tick\": [";
    bool first = true;
    for (const auto& r : lat) {
        if (r.v[0] <= 2 * tns) {
            j += std::string(first ? "" : ", ") + "\"" + r.name + "\"";
            first = false;
        }
    }
    j += "]\n}\n";

    // Markdown: one short table.
    auto cell = [&](const std::array<double, 3>& v) {
        char b[96];
        std::snprintf(b, sizeof b, "%.0f (%.0f–%.0f)%s", v[0], v[1], v[2], v[0] <= 2 * tns ? " ≈tick" : "");
        return std::string(b);
    };
    std::string md;
    md += std::string("**") + book + (k ? " K=" + std::to_string(k) : "") + ", N=" + std::to_string(b3::kPublishedDepth) +
          (b3::kTimed ? ", timed" : "") + "** on `" + path + "`" + (invalid ? " — **INVALID (battery or low power)**" : "") +
          "\n\n";
    char hd[256];
    std::snprintf(hd, sizeof hd, "%zu packets, %zu messages; %d passes; clock tick %.2f ns. Throughput %.2f M msg/s, %.2f M pkt/s (%.1f ns/msg; batch average %.1f ns/pkt).\n\n",
                  pk.size(), messages, passes, tns, 1e3 / msg[0], 1e3 / pkt[0], msg[0], bat[0]);
    md += hd;
    md += "| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |\n|---|---|---|---|---|---|\n";
    md += "| Port 1 (" + std::to_string(res[0].n1) + " packets) | " + cell(lat[0].v) + " | " + cell(lat[1].v) + " | " +
          cell(lat[2].v) + " | " + cell(lat[3].v) + " | " + cell(lat[4].v) + " |\n";
    md += "| Port 2 (" + std::to_string(res[0].n2) + " packets) | " + cell(lat[5].v) + " | " + cell(lat[6].v) + " | " +
          cell(lat[7].v) + " | " + cell(lat[8].v) + " | " + cell(lat[9].v) + " |\n";
    if (!out_prefix.empty()) {
        std::ofstream(out_prefix + ".json") << j;
        std::ofstream(out_prefix + ".md") << md;
    }
    std::fputs(md.c_str(), stdout);
    return 0;
}
