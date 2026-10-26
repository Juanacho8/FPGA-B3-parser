// test_layouts.cpp: the generated layouts.hpp against spec/LAYOUTS.md.
//
// What it does:
//   Checks a handful of values in include/b3/layouts.hpp against numbers
//   copied by hand from spec/LAYOUTS.md (section and table named next to each
//   check). The header is generated from B3's XML, LAYOUTS.md was verified
//   against B3's PDF, so this compares two independent sources.
//
// What it must not do:
//   Derive an expected value from the header itself. Every number on the
//   right-hand side is a literal from LAYOUTS.md.

#include <doctest.h>

#include <cstdint>
#include <limits>
#include <type_traits>

#include "b3/layouts.hpp"

namespace L = b3::layout;

TEST_CASE("headers (LAYOUTS.md sections 3 and 4)") {
    CHECK(L::packet_header::size == 16);
    CHECK(L::packet_header::sequenceNumber::offset == 4);
    CHECK(L::packet_header::sendingTime::offset == 8);
    CHECK(L::message_header::size == 12);
    CHECK(L::message_header::messageLength::offset == 0);
    CHECK(L::message_header::encodingType::offset == 2);
    CHECK(L::message_header::blockLength::offset == 4);
    CHECK(L::message_header::templateId::offset == 6);
    CHECK(L::message_header::schemaId::offset == 8);
    CHECK(L::message_header::version::offset == 10);
}

TEST_CASE("schema constants (LAYOUTS.md sources table, sections 2 and 4)") {
    CHECK(L::schema_id == 2);
    CHECK(L::schema_version == 17);
    CHECK(L::encoding_type == 0xEB50);
    CHECK(L::max_packet_size == 1400);
    CHECK(L::xml_sha256 == "b055b9a948756ab795f0f6f645a571094cacb85133e091e94440b812e17503c4");
}

TEST_CASE("Order_MBO_50 (LAYOUTS.md 7, Order_MBO_50)") {
    namespace T = L::Order_MBO_50;
    CHECK(T::template_id == 50);
    CHECK(T::block_length == 72);
    CHECK(T::mDEntryPx::offset == 12);
    CHECK(T::mDEntryPx::size == 8);
    CHECK(T::mDEntryPx::optional);
    CHECK(T::mDEntryPx::null_value == std::numeric_limits<std::int64_t>::min());
    CHECK(T::mDEntryPx::exponent == -4);
    CHECK(T::secondaryOrderID::offset == 44);
    CHECK(T::secondaryOrderID::size == 8);
    CHECK(T::rptSeq::offset == 52);
    CHECK(T::transactTime::offset == 56);
    CHECK(T::transactTime::since_version == 15);
    CHECK(T::mDEntryPrevSize::offset == 64);
    CHECK(T::mDEntryPrevSize::since_version == 16);
    CHECK(T::mDEntryPrevSize::null_value == std::numeric_limits<std::int64_t>::min());
    CHECK(T::enteringFirm::null_value == 0u);
    CHECK(T::mDEntryType::primitive == L::Primitive::character);
    static_assert(std::is_same_v<T::secondaryOrderID::type, std::uint64_t>);
    static_assert(std::is_same_v<T::mDEntryPx::type, std::int64_t>);
}

TEST_CASE("Trade_53 (LAYOUTS.md 7, Trade_53)") {
    namespace T = L::Trade_53;
    CHECK(T::block_length == 56);
    CHECK(T::tradeID::offset == 28);
    CHECK(T::tradeID::size == 4);
    CHECK(T::mDEntryBuyer::offset == 32);
    CHECK(T::mDEntrySeller::offset == 36);
    CHECK(T::transactTime::offset == 44);
    CHECK(T::transactTime::since_version == 15);
    CHECK(T::rptSeq::offset == 52);
    // Constants: 0 bytes on the wire, value from the schema.
    CHECK(T::mDEntryType::constant);
    CHECK(T::mDEntryType::size == 0);
    CHECK(T::mDEntryType::value == '2');
    CHECK(T::securityExchange::value == "BVMF");
}

TEST_CASE("DeleteOrder_MBO_51 (LAYOUTS.md 7, DeleteOrder_MBO_51)") {
    namespace T = L::DeleteOrder_MBO_51;
    CHECK(T::block_length == 52);
    CHECK(T::mDEntrySize::offset == 16);
    CHECK(T::secondaryOrderID::offset == 24);
    CHECK(T::mDEntryPx::offset == 44);
    CHECK(T::mDEntryPx::since_version == 15);
    CHECK(T::mDUpdateAction::constant);
    CHECK(T::mDUpdateAction::value == 2);
}

TEST_CASE("SnapshotFullRefresh_Orders_MBO_71 group (LAYOUTS.md 7)") {
    namespace T = L::SnapshotFullRefresh_Orders_MBO_71;
    CHECK(T::block_length == 8);
    CHECK(T::noMDEntries::dimension_size == 3);
    CHECK(T::noMDEntries::block_length == 42);
    CHECK(T::noMDEntries::secondaryOrderID::offset == 32);
    CHECK(T::noMDEntries::matchEventIndicator::offset == 41);
    CHECK(T::noMDEntries::matchEventIndicator::since_version == 10);
}

TEST_CASE("other layout details (LAYOUTS.md 7)") {
    CHECK(L::SettlementPrice_28::openCloseSettlFlag::deprecated);
    CHECK(L::SettlementPrice_28::openCloseSettlFlag::deprecated_since == 17);
    CHECK(L::SettlementPrice_28::priceType::null_value == 0);  // LAYOUTS.md 13
    CHECK(L::News_5::headline::prefix_size == 2);
    CHECK(L::SecurityDefinition_12::securityDesc::prefix_size == 1);
    CHECK(L::SecurityDefinition_12::symbol::size == 20);
    CHECK(L::SecurityDefinition_12::maturityMonthYear::members::month::offset == 2);
    CHECK(L::Sequence_2::nextSeqNo::offset == 0);
    CHECK(L::ExecutionSummary_55::block_length == 64);
    CHECK(L::template_ids.size() == 29);
}

TEST_CASE("sets and enums (LAYOUTS.md 10)") {
    CHECK(L::sets::MatchEventIndicator::bit::EndOfEvent == 7);
    CHECK(L::sets::MatchEventIndicator::bit::RecoveryMsg == 5);
    CHECK(L::sets::MatchEventIndicator::bit::Implied == 4);
    CHECK(L::sets::MatchEventIndicator::mask::EndOfEvent == 0x80);
    CHECK(L::sets::MatchEventIndicator::mask::RecoveryMsg == 0x20);
    CHECK(L::sets::TradeCondition::bit::RegularTrade == 13);
    CHECK(L::enums::MDUpdateAction::DELETE_THRU == 3);
    CHECK(L::enums::MDEntryType::OFFER == '1');
    CHECK(L::enums::AggressorSide::SELL == 2);
    CHECK(L::enums::PriceType::optional);
    CHECK(L::enums::PriceType::null_value == 0);
    static_assert(std::is_same_v<L::enums::MDEntryType::underlying_type, char>);
    static_assert(std::is_same_v<L::sets::TradeCondition::underlying_type, std::uint16_t>);
}
