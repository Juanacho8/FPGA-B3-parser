// test_decode.cpp: field access, presence rules, groups, var data, body faults
// and the decode writer, on messages built byte by byte.
//
// What it does:
//   Builds message bodies with values at offsets copied by hand from
//   spec/LAYOUTS.md section 7, then checks what decode::get, walk_body and
//   the DecodeWriter report: absent (past blockLength, or a later
//   sinceVersion), null, present; a longer blockLength than the schema's;
//   one or more templates of each family; and each body fault.
//
// What it must not do:
//   Take an offset or an expected value from layouts.hpp or fields.hpp.

#include <doctest.h>

#include <cstdint>
#include <cstdio>
#include <limits>
#include <string>
#include <vector>

#include "b3/decode.hpp"
#include "b3/decode_writer.hpp"
#include "b3/fields.hpp"

namespace F = b3::fields;
using b3::decode::get;
using b3::walker::MessageView;
using Bytes = std::vector<std::uint8_t>;

namespace {

constexpr std::int64_t kNull64 = std::numeric_limits<std::int64_t>::min();

void put(Bytes& b, std::size_t off, std::uint64_t v, int n) {
    if (b.size() < off + static_cast<std::size_t>(n)) b.resize(off + static_cast<std::size_t>(n), 0);
    for (int i = 0; i < n; ++i) b[off + static_cast<std::size_t>(i)] = static_cast<std::uint8_t>(v >> (8 * i));
}
void put_str(Bytes& b, std::size_t off, const std::string& s) {
    for (std::size_t i = 0; i < s.size(); ++i) put(b, off + i, static_cast<unsigned char>(s[i]), 1);
}

// A whole message: 12-byte header, body (root block), then tail (groups, var data).
struct Message {
    Bytes bytes;
    std::uint16_t template_id, version, block_length;
    MessageView view() const {
        return MessageView{{}, 0, template_id, version, block_length,
                           static_cast<std::uint16_t>(bytes.size()), bytes.data(), bytes.data() + 12};
    }
};

Message message(std::uint16_t tid, const Bytes& body, std::uint16_t version = 17,
                const Bytes& tail = {}, int block_length = -1) {
    Message m{{}, tid, version,
              static_cast<std::uint16_t>(block_length >= 0 ? block_length : static_cast<int>(body.size()))};
    const std::size_t len = 12 + body.size() + tail.size();
    put(m.bytes, 0, len, 2);
    put(m.bytes, 2, 0xEB50, 2);
    put(m.bytes, 4, m.block_length, 2);
    put(m.bytes, 6, tid, 2);
    put(m.bytes, 8, 2, 2);
    put(m.bytes, 10, version, 2);
    m.bytes.insert(m.bytes.end(), body.begin(), body.end());
    m.bytes.insert(m.bytes.end(), tail.begin(), tail.end());
    return m;
}

Bytes order_body(std::size_t size = 72) {
    Bytes b(size, 0);
    put(b, 0, 200000001, 8);           // securityID
    put(b, 8, 0x80, 1);                // matchEventIndicator
    put(b, 9, 1, 1);                   // mDUpdateAction CHANGE
    put(b, 10, '1', 1);                // mDEntryType OFFER
    put(b, 12, 12345678, 8);           // mDEntryPx
    put(b, 20, 10000, 8);              // mDEntrySize
    put(b, 32, 0, 4);                  // enteringFirm: null 0
    put(b, 36, 111, 8);                // mDInsertTimestamp
    put(b, 44, 42, 8);                 // secondaryOrderID
    put(b, 52, 7, 4);                  // rptSeq
    if (size >= 64) put(b, 56, 999, 8);                                  // transactTime
    if (size >= 72) put(b, 64, static_cast<std::uint64_t>(kNull64), 8);  // mDEntryPrevSize: null
    return b;
}

// Runs the writer on one message and returns its JSON line.
template <std::uint16_t Id>
std::string write_one(const Message& m, b3::decode_writer::DecodeWriter** keep = nullptr) {
    std::FILE* f = std::tmpfile();
    REQUIRE(f != nullptr);
    {
        b3::decode_writer::DecodeWriter w(f);
        w.on<Id>(m.view());
        if (keep) *keep = nullptr;
    }
    std::rewind(f);
    std::string out;
    for (int c; (c = std::fgetc(f)) != EOF;) out += static_cast<char>(c);
    std::fclose(f);
    return out;
}

bool has(const std::string& s, const std::string& part) { return s.find(part) != std::string::npos; }

}  // namespace

// ---- Presence rules ----

TEST_CASE("presence: present, null, absent past blockLength, absent by sinceVersion") {
    const auto v17 = message(50, order_body());
    const auto px = get<F::Order_MBO_50::mDEntryPx>(v17.view());
    CHECK(px.present());
    CHECK(px.value == 12345678);
    const auto prev = get<F::Order_MBO_50::mDEntryPrevSize>(v17.view());
    CHECK(prev.null());                // present on the wire, equal to INT64_MIN
    CHECK(prev.value == kNull64);
    CHECK(get<F::Order_MBO_50::enteringFirm>(v17.view()).null());   // FirmOptional null 0

    // Version 15, blockLength 64 (as the generator's schema set): mDEntryPrevSize
    // (offset 64, since 16) lies past blockLength and its version is later.
    const auto v15 = message(50, order_body(64), 15);
    CHECK(get<F::Order_MBO_50::mDEntryPrevSize>(v15.view()).absent());
    CHECK(get<F::Order_MBO_50::transactTime>(v15.view()).present());

    // Past blockLength alone: version 17, but a 64-byte root block.
    const auto short_block = message(50, order_body(64), 17);
    CHECK(get<F::Order_MBO_50::mDEntryPrevSize>(short_block.view()).absent());

    // sinceVersion alone: a full 72-byte block that says version 14.
    const auto old = message(50, order_body(72), 14);
    CHECK(get<F::Order_MBO_50::transactTime>(old.view()).absent());     // since 15
    CHECK(get<F::Order_MBO_50::mDEntryPrevSize>(old.view()).absent());  // since 16
    CHECK(get<F::Order_MBO_50::secondaryOrderID>(old.view()).present());
}

TEST_CASE("longer blockLength than the schema's: known fields read, extra bytes skipped") {
    Bytes body = order_body(80);       // version 18, 8 extra root bytes
    put(body, 72, 0xFFFFFFFFFFFFFFFFull, 8);
    Bytes tail;
    const auto m = message(50, body, 18);
    CHECK(m.view().block_length == 80);
    CHECK(get<F::Order_MBO_50::secondaryOrderID>(m.view()).value == 42);
    CHECK(get<F::Order_MBO_50::mDEntryPrevSize>(m.view()).null());
    const auto line = write_one<50>(m);
    CHECK(has(line, "\"blockLength\":80,\"version\":18"));
    CHECK(has(line, "\"secondaryOrderID\":42"));
    CHECK_FALSE(has(line, "absent"));
}

TEST_CASE("constant fields come from the schema") {
    CHECK(b3::decode::constant<F::Trade_53::mDEntryType>() == '2');
    CHECK(b3::decode::constant<F::DeleteOrder_MBO_51::mDUpdateAction>() == 2);
    CHECK(b3::decode::constant<F::Trade_53::securityExchange>() == "BVMF");
}

// ---- Template families ----

TEST_CASE("book: Order_MBO, DeleteOrder_MBO, MassDeleteOrders_MBO") {
    const auto order = message(50, order_body());
    CHECK(get<F::Order_MBO_50::mDEntryType>(order.view()).value == '1');
    CHECK(get<F::Order_MBO_50::mDUpdateAction>(order.view()).value == 1);
    CHECK(get<F::Order_MBO_50::rptSeq>(order.view()).value == 7u);

    Bytes del(52, 0);
    put(del, 0, 5, 8);
    put(del, 10, '0', 1);
    put(del, 16, 300, 8);              // mDEntrySize
    put(del, 24, 77, 8);               // secondaryOrderID
    put(del, 32, 1234, 8);             // transactTime
    put(del, 40, 9, 4);                // rptSeq
    put(del, 44, static_cast<std::uint64_t>(-5), 8);   // mDEntryPx, negative
    const auto d = message(51, del);
    CHECK(get<F::DeleteOrder_MBO_51::mDEntrySize>(d.view()).value == 300);
    CHECK(get<F::DeleteOrder_MBO_51::mDEntryPx>(d.view()).value == -5);
    CHECK(get<F::DeleteOrder_MBO_51::rptSeq>(d.view()).value == 9u);

    Bytes mass(28, 0);
    put(mass, 9, 3, 1);                // DELETE_THRU
    put(mass, 10, '1', 1);
    put(mass, 16, 55, 8);
    put(mass, 24, 3, 4);
    const auto ms = message(52, mass);
    CHECK(get<F::MassDeleteOrders_MBO_52::mDUpdateAction>(ms.view()).value == 3);
    CHECK(get<F::MassDeleteOrders_MBO_52::transactTime>(ms.view()).value == 55u);
}

TEST_CASE("trades: Trade, ExecutionSummary, TradeBust") {
    Bytes t(56, 0);
    put(t, 12, 385100, 8);
    put(t, 20, 100, 8);
    put(t, 28, 3, 4);                  // tradeID
    put(t, 32, 15, 4);                 // mDEntryBuyer
    put(t, 36, 14, 4);                 // mDEntrySeller
    put(t, 40, 20745, 2);              // tradeDate
    put(t, 42, 0, 1);                  // trdSubType: null 0
    put(t, 44, 77, 8);
    const auto tr = message(53, t);
    CHECK(get<F::Trade_53::tradeID>(tr.view()).value == 3u);
    CHECK(get<F::Trade_53::mDEntrySeller>(tr.view()).value == 14u);
    CHECK(get<F::Trade_53::tradeDate>(tr.view()).value == 20745);
    CHECK(get<F::Trade_53::trdSubType>(tr.view()).null());
    CHECK(get<F::Trade_53::transactTime>(tr.view()).value == 77u);

    Bytes es(64, 0);
    put(es, 10, 2, 1);                 // aggressorSide SELL
    put(es, 12, 385100, 8);
    put(es, 20, 100, 8);
    put(es, 28, static_cast<std::uint64_t>(kNull64), 8);   // tradedHiddenQty: null
    const auto e = message(55, es);
    CHECK(get<F::ExecutionSummary_55::aggressorSide>(e.view()).value == 2);
    CHECK(get<F::ExecutionSummary_55::tradedHiddenQty>(e.view()).null());
    CHECK(get<F::ExecutionSummary_55::cxlQty>(e.view()).present());     // 0 is not its null

    Bytes tb(48, 0);
    put(tb, 28, 147, 4);
    put(tb, 32, 20745, 2);
    put(tb, 44, 1167, 4);
    const auto b = message(57, tb);
    CHECK(get<F::TradeBust_57::tradeID>(b.view()).value == 147u);
    CHECK(get<F::TradeBust_57::rptSeq>(b.view()).value == 1167u);
}

TEST_CASE("statistics: ExecutionStatistics, SettlementPrice (unaligned, deprecated)") {
    Bytes st(52, 0);
    put(st, 12, 600, 8);               // tradeVolume
    put(st, 20, 385117, 8);            // vwapPx
    put(st, 28, static_cast<std::uint64_t>(kNull64), 8);   // netChgPrevDay: null
    put(st, 36, 3, 4);                 // numberOfTrades
    put(st, 40, 2000, 8);              // mDEntryTimestamp
    const auto s = message(56, st);
    CHECK(get<F::ExecutionStatistics_56::vwapPx>(s.view()).value == 385117);
    CHECK(get<F::ExecutionStatistics_56::netChgPrevDay>(s.view()).null());
    CHECK(get<F::ExecutionStatistics_56::numberOfTrades>(s.view()).value == 3u);

    Bytes sp(40, 0);
    put(sp, 12, 1300000000, 8);
    put(sp, 28, 255, 1);               // openCloseSettlFlag: deprecated v17, written as null
    put(sp, 29, 2, 1);                 // priceType PU
    put(sp, 30, 1, 1);                 // settlPriceType FINAL
    put(sp, 31, 0xA1B2C3D4u, 4);       // rptSeq at an odd offset
    put(sp, 35, 20744, 2);             // settlDate, since v17
    const auto p = message(28, sp);
    CHECK(get<F::SettlementPrice_28::openCloseSettlFlag>(p.view()).null());
    CHECK(get<F::SettlementPrice_28::rptSeq>(p.view()).value == 0xA1B2C3D4u);
    CHECK(get<F::SettlementPrice_28::settlDate>(p.view()).value == 20744);
    const auto p16 = message(28, sp, 16);
    CHECK(get<F::SettlementPrice_28::settlDate>(p16.view()).absent());
}

TEST_CASE("session and resets: SecurityGroupPhase (char[3]), EmptyBook, ChannelReset, Sequence") {
    Bytes g(32, 0);
    put_str(g, 0, "IND");
    put(g, 9, 1, 1);
    put(g, 10, 17, 1);
    put(g, 11, 4, 1);
    put(g, 12, 20745, 2);
    put(g, 24, 42, 8);
    const auto ph = message(10, g);
    CHECK(get<F::SecurityGroupPhase_10::securityGroup>(ph.view()).value == "IND");
    CHECK(get<F::SecurityGroupPhase_10::tradSesOpenTime>(ph.view()).null());
    const auto line = write_one<10>(ph);
    CHECK(has(line, "\"securityGroup\":\"IND\""));
    CHECK(has(line, "\"null\":[\"tradSesOpenTime\"]"));

    Bytes eg(32, 0);
    put_str(eg, 0, "EQ");              // char[3] padded with NUL: "EQ"
    CHECK(get<F::SecurityGroupPhase_10::securityGroup>(message(10, eg).view()).value == "EQ");

    Bytes e(20, 0);
    put(e, 0, 7, 8);
    put(e, 8, 0x20, 1);
    put(e, 12, 99, 8);
    CHECK(get<F::EmptyBook_9::mDEntryTimestamp>(message(9, e).view()).value == 99u);
    Bytes c(12, 0);
    put(c, 0, 0xA0, 1);
    put(c, 4, 98, 8);
    CHECK(get<F::ChannelReset_11::matchEventIndicator>(message(11, c).view()).value == 0xA0);
    Bytes q(4, 0);
    put(q, 0, 890, 4);
    CHECK(get<F::Sequence_2::nextSeqNo>(message(2, q).view()).value == 890u);
    const auto reset = write_one<1>(message(1, {}));
    CHECK(has(reset, "\"fields\":{},\"null\":[]"));
}

// ---- Var data ----

Bytes news_body(std::uint16_t part, std::uint16_t count) {
    Bytes n(36, 0);
    put(n, 8, 0x80, 1);
    put_str(n, 10, "en");
    put(n, 12, count, 2);
    put(n, 14, part, 2);
    put(n, 24, 5, 8);
    put(n, 32, 9, 4);
    return n;
}
Bytes var(const std::string& s, int prefix = 2) {
    Bytes b;
    put(b, 0, s.size(), prefix);
    put_str(b, static_cast<std::size_t>(prefix), s);
    return b;
}
Bytes cat(std::initializer_list<Bytes> parts) {
    Bytes out;
    for (const auto& p : parts) out.insert(out.end(), p.begin(), p.end());
    return out;
}

TEST_CASE("News: var data after the header's blockLength, text and news reassembly") {
    const auto one = message(5, news_body(1, 1), 17, cat({var("Aviso 2"), var("hello \"x\""), var("")}));
    CHECK(get<F::News_5::securityID>(one.view()).null());   // SecurityIDOptional null 0
    CHECK(get<F::News_5::languageCode>(one.view()).value == "en");
    const auto line = write_one<5>(one);
    CHECK(has(line, "\"headline\":\"Aviso 2\",\"text\":\"hello \\\"x\\\"\",\"uRLLink\":\"\""));
    CHECK(has(line, "\"news\":{\"headline\":\"Aviso 2\",\"text\":\"hello \\\"x\\\"\"}"));

    // Version 18 with 8 extra root bytes: var data starts at blockLength 44.
    Bytes ext = news_body(1, 1);
    ext.resize(44, 0xEE);
    const auto v18 = message(5, ext, 18, cat({var("H"), var("T"), var("U")}));
    const auto l18 = write_one<5>(v18);
    CHECK(has(l18, "\"headline\":\"H\",\"text\":\"T\",\"uRLLink\":\"U\""));

    // Three parts on one channel: only the last carries "news", text joined.
    std::FILE* f = std::tmpfile();
    {
        b3::decode_writer::DecodeWriter w(f);
        w.on<5>(message(5, news_body(1, 3), 17, cat({var("Big"), var("ab"), var("")})).view());
        w.on<5>(message(5, news_body(2, 3), 17, cat({var(""), var("cd"), var("")})).view());
        w.on<5>(message(5, news_body(3, 3), 17, cat({var(""), var("ef"), var("")})).view());
    }
    std::rewind(f);
    std::string all;
    for (int ch; (ch = std::fgetc(f)) != EOF;) all += static_cast<char>(ch);
    std::fclose(f);
    CHECK(all.find("\"news\"") == all.rfind("\"news\""));     // exactly once
    CHECK(has(all, "\"news\":{\"headline\":\"Big\",\"text\":\"abcdef\"}"));
}

// ---- Groups and composites ----

TEST_CASE("snapshot orders: group after blockLength, entries of the wire's blockLength") {
    Bytes root(8, 0);
    put(root, 0, 100000001, 8);
    Bytes tail;
    put(tail, 0, 44, 2);               // entry blockLength 44: 2 bytes more than the schema's
    put(tail, 2, 2, 1);                // numInGroup
    for (int i = 0; i < 2; ++i) {
        Bytes e(44, 0xCC);
        put(e, 0, 385100 + static_cast<std::uint64_t>(i), 8);   // mDEntryPx
        put(e, 8, 100, 8);
        put(e, 20, 15, 4);
        put(e, 32, 1000 + static_cast<std::uint64_t>(i), 8);    // secondaryOrderID
        put(e, 40, '0', 1);
        put(e, 41, 0, 1);
        tail.insert(tail.end(), e.begin(), e.end());
    }
    const auto m = message(71, root, 17, tail);
    const auto line = write_one<71>(m);
    CHECK(has(line, "\"noMDEntries\":[{\"fields\":{\"mDEntryPx\":385100,"));
    CHECK(has(line, "\"secondaryOrderID\":1001"));
    CHECK_FALSE(has(line, "body_fault"));

    // Version 9: the entries' matchEventIndicator (since v10) is absent.
    const auto v9 = write_one<71>(message(71, root, 9, tail));
    CHECK(has(v9, "\"absent\":[\"matchEventIndicator\"]"));
}

TEST_CASE("SecurityDefinition: composite members, three groups, 1-byte var data prefix") {
    Bytes root(232, 0);
    put(root, 0, 7, 8);
    put_str(root, 16, "WINZ26");
    put(root, 188, 2026, 2);           // maturityMonthYear.year
    put(root, 190, 12, 1);             // month
    Bytes tail = cat({{3, 0, 0}, {3, 0, 0}, {2, 0, 0}, var("desc", 1)});   // three empty groups
    tail[0] = 28;
    tail[3] = 38;
    tail[6] = 2;
    const auto line = write_one<12>(message(12, root, 17, tail));
    CHECK(has(line, "\"symbol\":\"WINZ26\""));
    CHECK(has(line, "\"maturityMonthYear\":{\"year\":2026,\"month\":12,\"day\":0,\"week\":0}"));
    CHECK(has(line, "\"contractSettlMonth\":{\"year\":0,\"month\":0,\"day\":0,\"week\":0}"));
    CHECK(has(line, "\"noUnderlyings\":[],\"noLegs\":[],\"noInstrAttribs\":[],\"securityDesc\":\"desc\""));
    CHECK(has(line, "\"contractSettlMonth\""));
    CHECK_FALSE(has(line, "body_fault"));
}

// ---- Body faults ----

struct FaultCase {
    const char* name;
    Bytes tail;
    std::size_t reason;
};

TEST_CASE("body faults: counted by reason, decoded part reported, next message still written") {
    Bytes root(8, 0);
    const Bytes entry(42, 0);
    std::vector<std::pair<Message, std::size_t>> cases;
    // 1. Group header does not fit: 2 of its 3 bytes.
    cases.push_back({message(71, root, 17, {42, 0}), 0});
    // 2. Second entry runs past the end.
    {
        Bytes t = {42, 0, 2};
        t.insert(t.end(), entry.begin(), entry.end());
        t.insert(t.end(), entry.begin(), entry.begin() + 10);
        cases.push_back({message(71, root, 17, t), 1});
    }
    // 3. News: the text's length prefix does not fit (1 of 2 bytes).
    cases.push_back({message(5, news_body(1, 1), 17, cat({var("H"), {5}})), 2});
    // 4. News: text says 50 bytes, 3 follow.
    cases.push_back({message(5, news_body(1, 1), 17, cat({var("H"), {50, 0, 'a', 'b', 'c'}})), 3});

    for (const auto& [m, reason] : cases) {
        CAPTURE(reason);
        std::FILE* f = std::tmpfile();
        std::array<std::uint64_t, b3::decode::kBodyFaults> faults{};
        {
            b3::decode_writer::DecodeWriter w(f);
            if (m.template_id == 71) w.on<71>(m.view()); else w.on<5>(m.view());
            w.on<2>(message(2, {1, 0, 0, 0}).view());       // the next message
            faults = w.body_faults();
            CHECK(w.rows() == 2);
        }
        std::rewind(f);
        std::string all;
        for (int ch; (ch = std::fgetc(f)) != EOF;) all += static_cast<char>(ch);
        std::fclose(f);
        for (std::size_t r = 0; r < faults.size(); ++r) CHECK(faults[r] == (r == reason ? 1u : 0u));
        CHECK(has(all, std::string("\"reason\":\"") + std::string(b3::decode::kBodyFaultNames[reason]) + "\""));
        CHECK(has(all, "\"nextSeqNo\":1"));                 // framing intact: next message decoded
        if (reason == 1) {
            CHECK(has(all, "\"noMDEntries\":[{\"fields\""));      // the first entry is reported
            CHECK(has(all, "\"undecoded\":[\"noMDEntries\"]"));
        }
        if (reason >= 2) {
            CHECK(has(all, "\"headline\":\"H\""));                // decoded before the fault
            CHECK(has(all, "\"undecoded\":[\"text\",\"uRLLink\"]"));
            CHECK_FALSE(has(all, "\"news\""));
        }
    }
}

TEST_CASE("unknown template: hex body") {
    std::FILE* f = std::tmpfile();
    {
        b3::decode_writer::DecodeWriter w(f);
        auto m = message(999, {0x0c, 0x5b, 0xff}, 18);
        w.on_unknown(m.view());
    }
    std::rewind(f);
    std::string all;
    for (int ch; (ch = std::fgetc(f)) != EOF;) all += static_cast<char>(ch);
    std::fclose(f);
    CHECK(has(all, "\"templateId\":999,\"blockLength\":3,\"version\":18,\"fields\":{\"body\":\"0c5bff\"},\"null\":[]}"));
}
