# B3 Binary UMDF: wire format and feed behavior

This file describes only what B3 publishes. Nothing in here is a project design choice.

**Sources (all public, B3):**

| File | Version | Use |
|---|---|---|
| `b3-market-data-messages-2.3.1.xml` | schema id 2, version 17, semantic 2.3.1 | Source of truth for every byte layout |
| `BinaryUMDF-MessageReference-v.2.3.1-enUS.pdf` | 2.3.1 (Jun 18, 2026) | Cross-check of every offset and length |
| `BinaryUMDF-MessageSpecificationGuidelines-v.2.3.1-enUS.pdf` | 2.3.1 | Feed behavior (sections 6 to 12 below) |

XML SHA-256: `b055b9a948756ab795f0f6f645a571094cacb85133e091e94440b812e17503c4`

**How the layouts were verified**

1. Every field offset and blockLength was computed mechanically from the XML using SBE 1.0 rules (fields are packed in order, an `offset` attribute forces a position, a message `blockLength` attribute forces trailing padding, constants take 0 bytes).
2. All 29 templates were compared against the Message Reference PDF, field by field. Every blockLength and every field offset/size matches, except the documentation errors listed in section 13.
3. The sample packet printed in the Message Reference (section 6, Order_50 + Trade_53) was decoded byte by byte with these offsets and every field lands on a sensible value.

---

## 1. Conventions

* All integers are **little-endian**, including the packet header.
* Offsets in message tables are relative to the start of the message **body** (the byte right after the 12-byte message header). Add 12 for the offset inside the message, and add the message's position in the packet for the offset inside the datagram.
* `P` column: R required, O optional, C constant.
* `Null` column: the encoded value that means "absent" for that type. Optional fields always occupy their full size on the wire, null or not.
* Prices are fixed-point decimals: the wire carries an int64 mantissa, the exponent is a schema constant (not sent). `Price` = mantissa x 10^-4, so 12345678 means 1234.5678. `Price8`/`Fixed8` use 10^-8.
* `char[N]` strings are ASCII, padded on the right with `0x00`.
* `UTCTimestampNanos` = uint64 nanoseconds since Unix epoch. `LocalMktDate` = uint16 days since Unix epoch.

## 2. Transport

* UDP multicast. One UDP datagram = one B3 packet.
* Packet size is at most **1400 bytes** including the 16-byte packet header.
* A packet holds one packet header followed by **one or more complete messages**. A message never spans two packets.
* Messages are back to back with **no padding between them** (confirmed by the sample packet: message 2 starts at the byte right after message 1 ends).
* A packet can contain messages from several events, and one event can span several packets.

## 3. Packet header (16 bytes, once per datagram)

| Off | Size | Field | Type | Meaning |
|---|---|---|---|---|
| 0 | 1 | channelNumber | uint8 | Channel id |
| 1 | 1 | reserved | uint8 | Reserved |
| 2 | 2 | sequenceVersion | uint16 | Incremental stream: starts at 1, incremented weekly or on market data engine failover. Snapshot and instrument definition streams: changes every loop. Rolls back to 1 after 65534 |
| 4 | 4 | sequenceNumber | uint32 | Packet sequence number, +1 per packet within the same channel and sequenceVersion. Resets to 1 when sequenceVersion changes. **0 in heartbeat (Sequence) packets** |
| 8 | 8 | sendingTime | uint64 | Transmission time, ns since epoch (microsecond-level precision) |

## 4. Message header (12 bytes, once per message)

| Off | Size | Field | Type | Meaning |
|---|---|---|---|---|
| 0 | 2 | messageLength | uint16 | Whole message length **including** this 12-byte header and any groups/var data |
| 2 | 2 | encodingType | uint16 | Always `0xEB50` (bytes `50 EB`): SBE 1.0 little-endian |
| 4 | 2 | blockLength | uint16 | Size of the root block (fixed part) of the body |
| 6 | 2 | templateId | uint16 | Which message |
| 8 | 2 | schemaId | uint16 | Always 2 |
| 10 | 2 | version | uint16 | Schema version used to encode this message (17 today) |

Bytes 0 to 3 are the framing header (compact SOFH). Bytes 4 to 11 are the SBE message header.

For messages with repeating groups, each group starts with a 3-byte dimension header (`blockLength` uint16, `numInGroup` uint8) followed by `numInGroup` entries of `blockLength` bytes. Variable-length data is a length prefix followed by that many bytes.

## 5. Decoding rules B3 mandates

From Guidelines 5.9 and 5.10:

* **Always use `blockLength` and `version` from the message header**, never the value assumed from the template. Newer compatible versions of the same template append fields at the end of the root block, which makes blockLength larger and shifts any groups to the right.
* Advance to the next message with `messageLength` (or blockLength plus groups/data).
* If the received `version` is older than your decoder, fields whose `sinceVersion` is greater than the received version are not present.
* If it is newer, decode the fields you know and skip the rest using blockLength.
* Breaking changes get a **new templateId**, and the old one is marked deprecated. During a rollout, different channels can publish different schema versions, so decoders should accept both the old and new template.
* Unknown templates are skipped by messageLength.

---

## 6. Template index

`Streams`: I incremental, S snapshot recovery, D instrument definition (from Guidelines 6 and 7.1).

| ID | Name | blockLength | Streams | Purpose |
|---|---|---|---|---|
| 1 | SequenceReset | 0 | I, S, D | Stream reset; end of every snapshot and definition loop |
| 2 | Sequence | 4 | I, S, D | Heartbeat, about every 1 s of inactivity |
| 3 | SecurityStatus | 36 | I, S | Instrument trading status |
| 5 | News | 36 + var data | I | News, split into parts if large |
| 9 | EmptyBook | 20 | I | Clear one instrument's book |
| 10 | SecurityGroupPhase | 32 | I, S | Trading phase of a security group |
| 11 | ChannelReset | 12 | I | Clear all books in the channel |
| 12 | SecurityDefinition | 232 + groups + var data | D, I | Instrument definition |
| 15 | OpeningPrice | 44 | I, S | Statistic |
| 16 | TheoreticalOpeningPrice | 40 | I, S | Statistic (auctions) |
| 17 | ClosingPrice | 36 | I, S | Statistic |
| 19 | AuctionImbalance | 32 | I, S | Auction imbalance |
| 21 | QuantityBand | 40 | I, S | Quantity limits |
| 22 | PriceBand | 48 | I, S | Price tunnel |
| 24 | HighPrice | 32 | I, S | Session high |
| 25 | LowPrice | 32 | I, S | Session low |
| 27 | LastTradePrice | 68 | I, S | Last trade |
| 28 | SettlementPrice | 40 | I, S | Settlement |
| 29 | OpenInterest | 32 | I, S | Open interest |
| 30 | SnapshotFullRefresh_Header | 34 | S | Start of one instrument's snapshot |
| 50 | Order_MBO | 72 | I | Add or change one order |
| 51 | DeleteOrder_MBO | 52 | I | Delete one order |
| 52 | MassDeleteOrders_MBO | 28 | I | Delete a whole side of a book |
| 53 | Trade | 56 | I | One trade |
| 54 | ForwardTrade | 68 | I | Trade on a forward ("termo") instrument |
| 55 | ExecutionSummary | 64 | I | First message of a matching event |
| 56 | ExecutionStatistics | 52 | I, S | Volume, VWAP, number of trades |
| 57 | TradeBust | 48 | I | Trade cancelled by market supervision |
| 71 | SnapshotFullRefresh_Orders_MBO | 8 + group (42 per entry) | S | Orders of one instrument's snapshot |

The XML also defines `HeaderMessage_0` (id 0). It is a code-generation convenience that wraps the packet and framing headers. B3 never sends it as a message.

---

## 7. Message layouts

Generated from the XML. Constant fields take 0 bytes and are listed above each table with their implied value.

### 1 SequenceReset_1

blockLength **0**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = SequenceReset = '4', applVerID(1128) = FIX50SP2 = 9, newSeqNo(36) = 1


### 2 Sequence_2

blockLength **4**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = Sequence = '0', applVerID(1128) = FIX50SP2 = 9

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 4 | nextSeqNo | 35526 | SeqNum (uint32) | R |  |  |


### 3 SecurityStatus_3

blockLength **36**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = SecurityStatus = 'f', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | tradingSessionID | 336 | TradingSessionID enum (uint8) | R |  |  |
| 10 | 1 | securityTradingStatus | 326 | SecurityTradingStatus enum (uint8) | R |  |  |
| 11 | 1 | securityTradingEvent | 1174 | SecurityTradingEvent enum (uint8) | O | 255 |  |
| 12 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 14 | 2 | *padding* | | | | | |
| 16 | 8 | tradSesOpenTime | 342 | UTCTimestampNanos (uint64 ns since epoch) | O | 0 |  |
| 24 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 32 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 5 News_5

blockLength **36** + groups/var data. messageLength = 12 + blockLength + groups/var data.

Constants (0 bytes on the wire): messageType(35) = News = 'B', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityIDOptional (uint64) | O | 0 |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | newsSource | 6940 | NewsSource enum (uint8) | R |  |  |
| 10 | 2 | languageCode | 1474 | LanguageCode (char[2]) | O | 0 |  |
| 12 | 2 | partCount | 37709 | UInt16 (uint16) | R |  |  |
| 14 | 2 | partNumber | 37710 | UInt16 (uint16) | R |  |  |
| 16 | 8 | newsID | 1472 | NewsID (uint64) | O | 0 |  |
| 24 | 8 | origTime | 42 | UTCTimestampNanos (uint64 ns since epoch) | O | 0 |  |
| 32 | 4 | totalTextLength | 37777 | UInt32 (uint32) | R |  |  |

Var data **headline** (148): 2-byte uint16 length prefix, then that many bytes (VarString).

Var data **text** (58): 2-byte uint16 length prefix, then that many bytes (VarString).

Var data **uRLLink** (149): 2-byte uint16 length prefix, then that many bytes (VarString).


### 9 EmptyBook_9

blockLength **20**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = EMPTY_BOOK = 'J', securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 3 | *padding* | | | | | |
| 12 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |


### 10 SecurityGroupPhase_10

blockLength **32**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = SecurityStatus = 'f', applVerID(1128) = FIX50SP2 = 9

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 3 | securityGroup | 1151 | SecurityGroup (char[3]) | R |  |  |
| 3 | 5 | *padding* | | | | | |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | tradingSessionID | 336 | TradingSessionID enum (uint8) | R |  |  |
| 10 | 1 | tradingSessionSubID | 625 | TradingSessionSubID enum (uint8) | R |  |  |
| 11 | 1 | securityTradingEvent | 1174 | SecurityTradingEvent enum (uint8) | O | 255 |  |
| 12 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 14 | 2 | *padding* | | | | | |
| 16 | 8 | tradSesOpenTime | 342 | UTCTimestampNanos (uint64 ns since epoch) | O | 0 |  |
| 24 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |


### 11 ChannelReset_11

blockLength **12**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = EMPTY_BOOK = 'J'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 1 | 3 | *padding* | | | | | |
| 4 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |


### 12 SecurityDefinition_12

blockLength **232** + groups/var data. messageLength = 12 + blockLength + groups/var data. (since v8)

Constants (0 bytes on the wire): messageType(35) = SecurityDefinition = 'd', applVerID(1128) = FIX50SP2 = 9

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 4 | securityExchange | 207 | SecurityExchange (char[4]) | R |  |  |
| 12 | 1 | securityIDSource | 22 | SecurityIDSource enum (char) | R |  |  |
| 13 | 3 | securityGroup | 1151 | SecurityGroup (char[3]) | R |  |  |
| 16 | 20 | symbol | 55 | Symbol (char[20]) | R |  |  |
| 36 | 1 | securityUpdateAction | 980 | SecurityUpdateAction enum (char) | R |  |  |
| 37 | 1 | securityType | 167 | SecurityType enum (uint8) | R |  |  |
| 38 | 2 | securitySubType | 762 | UInt16 (uint16) | R |  |  |
| 40 | 4 | totNoRelatedSym | 393 | UInt32 (uint32) | R |  |  |
| 44 | 8 | minPriceIncrement | 969 | Fixed8 (int64, x10^-8) | O | -9223372036854775808 |  |
| 52 | 8 | strikePrice | 202 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 |  |
| 60 | 8 | contractMultiplier | 231 | Fixed8 (int64, x10^-8) | O | -9223372036854775808 |  |
| 68 | 8 | priceDivisor | 37012 | Fixed8 (int64, x10^-8) | O | -9223372036854775808 |  |
| 76 | 8 | securityValidityTimestamp | 6938 | UTCTimestampSeconds (int64 s since epoch) | R | -9223372036854775808 |  |
| 84 | 8 | noSharesIssued | 7595 | UInt64NULL (uint64) | O | 0 |  |
| 92 | 8 | clearingHouseID | 37037 | ClearingHouseID (uint64) | O | 0 |  |
| 100 | 8 | minOrderQty | 9749 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 108 | 8 | maxOrderQty | 9748 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 116 | 8 | minLotSize | 1231 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 124 | 8 | minTradeVol | 562 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 132 | 4 | corporateActionEventId | 37010 | UInt32NULL (uint32) | O | 0 |  |
| 136 | 4 | issueDate | 225 | LocalMktDate32 (int32) | R |  |  |
| 140 | 4 | maturityDate | 541 | LocalMktDate32Optional (int32) | O | 0 |  |
| 144 | 2 | countryOfIssue | 470 | CountryCode (char[2]) | O | 0 |  |
| 146 | 4 | startDate | 916 | LocalMktDate32Optional (int32) | O | 0 |  |
| 150 | 4 | endDate | 917 | LocalMktDate32Optional (int32) | O | 0 |  |
| 154 | 2 | settlType | 63 | SettlType (uint16) | O | 65535 |  |
| 156 | 4 | settlDate | 64 | LocalMktDate32Optional (int32) | O | 0 |  |
| 160 | 4 | datedDate | 873 | LocalMktDate32Optional (int32) | O | 0 |  |
| 164 | 12 | isinNumber | 37026 | ISINNumber (char[12]) | O | 0 |  |
| 176 | 6 | asset | 6937 | Asset (char[6]) | R |  |  |
| 182 | 6 | cfiCode | 461 | CFICode (char[6]) | R |  |  |
| 188 | 5 | maturityMonthYear | 200 | MaturityMonthYear (year uint16, month uint8, day uint8, week uint8) | O |  |  |
| 193 | 5 | contractSettlMonth | 667 | MaturityMonthYear (year uint16, month uint8, day uint8, week uint8) | O |  |  |
| 198 | 3 | currency | 15 | Currency (char[3]) | R |  |  |
| 201 | 3 | strikeCurrency | 947 | Currency (char[3]) | O | 0 |  |
| 204 | 3 | settlCurrency | 120 | Currency (char[3]) | O | 0 |  |
| 207 | 3 | securityStrategyType | 7534 | SecurityStrategyType (char[3]) | O | 0 |  |
| 210 | 1 | lotType | 1093 | LotType enum (uint8) | O | 255 |  |
| 211 | 1 | tickSizeDenominator | 5151 | UInt8 (uint8) | O | 255 |  |
| 212 | 1 | product | 460 | Product enum (uint8) | R |  |  |
| 213 | 1 | exerciseStyle | 1194 | ExerciseStyle enum (uint8) | O | 255 |  |
| 214 | 1 | putOrCall | 201 | PutOrCall enum (uint8) | O | 255 |  |
| 215 | 1 | priceType | 423 | PriceType enum (uint8) | O | 0 |  |
| 216 | 1 | marketSegmentID | 1300 | MarketSegmentID (uint8) | O | 0 |  |
| 217 | 1 | governanceIndicator | 37011 | GovernanceIndicator enum (uint8) | O | 255 |  |
| 218 | 1 | securityMatchType | 37015 | SecurityMatchType enum (uint8) | O | 255 |  |
| 219 | 1 | lastFragment | 893 | Boolean enum (uint8) | O | 255 |  |
| 220 | 1 | multiLegModel | 1377 | MultiLegModel enum (uint8) | O | 255 |  |
| 221 | 1 | multiLegPriceMethod | 1378 | MultiLegPriceMethod enum (uint8) | O | 255 |  |
| 222 | 8 | minCrossQty | 35561 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 230 | 1 | impliedMarketIndicator | 1144 | ImpliedMarketIndicator enum (uint8) | O | 255 | since v10 |
| 231 | 1 | optPayoutType | 1482 | OptPayoutType enum (uint8) | O | 0 | since v16 |

Group **noUnderlyings** (711), dimension GroupSizeEncoding (3 B: blockLength uint16, numInGroup uint8), entry blockLength **28**. Offsets relative to each entry.

Group constants: underlyingSecurityIDSource(305) = EXCHANGE_SYMBOL = '8', underlyingSecurityExchange(308) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | underlyingSecurityID | 309 | SecurityID (uint64) | R |  |  |
| 8 | 20 | underlyingSymbol | 311 | Symbol (char[20]) | R |  |  |

Group **noLegs** (555), dimension GroupSizeEncoding (3 B: blockLength uint16, numInGroup uint8), entry blockLength **38**. Offsets relative to each entry.

Group constants: legSecurityIDSource(603) = EXCHANGE_SYMBOL = '8', legSecurityExchange(616) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | legSecurityID | 602 | SecurityID (uint64) | R |  |  |
| 8 | 8 | legRatioQty | 623 | RatioQty (int64, x10^-7) | R | -9223372036854775808 |  |
| 16 | 1 | legSecurityType | 609 | SecurityType enum (uint8) | R |  |  |
| 17 | 1 | legSide | 624 | Side enum (uint8) | R |  |  |
| 18 | 20 | legSymbol | 600 | Symbol (char[20]) | R |  |  |

Group **noInstrAttribs** (870), dimension GroupSizeEncoding (3 B: blockLength uint16, numInGroup uint8), entry blockLength **2**. Offsets relative to each entry.

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 1 | instrAttribType | 871 | InstrAttribType enum (uint8) | R |  |  |
| 1 | 1 | instrAttribValue | 872 | InstrAttribValue enum (uint8) | R |  |  |

Var data **securityDesc** (107): 1-byte uint8 length prefix, then that many bytes (TextEncoding).


### 15 OpeningPrice_15

blockLength **44**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDEntryType(269) = OPENING_PRICE = '4'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | mDUpdateAction | 279 | MDUpdateAction enum (uint8) | R |  |  |
| 10 | 1 | openCloseSettlFlag | 286 | OpenCloseSettlFlag enum (uint8) | R |  |  |
| 11 | 1 | *padding* | | | | | |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | netChgPrevDay | 451 | PriceOffset8Optional (int64, x10^-8) | O | -9223372036854775808 |  |
| 28 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 30 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 38 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |
| 42 | 2 | *padding* | | | | | |


### 16 TheoreticalOpeningPrice_16

blockLength **40**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDEntryType(269) = OPENING_PRICE = '4', openCloseSettlFlag(286) = THEORETICAL_PRICE = 5

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | mDUpdateAction | 279 | MDUpdateAction enum (uint8) | R |  |  |
| 10 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 12 | 8 | mDEntryPx | 270 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 |  |
| 20 | 8 | mDEntrySize | 271 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 28 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 36 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 17 ClosingPrice_17

blockLength **36**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = CLOSING_PRICE = '5'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | openCloseSettlFlag | 286 | OpenCloseSettlFlag enum (uint8) | R |  |  |
| 10 | 2 | *padding* | | | | | |
| 12 | 8 | mDEntryPx | 270 | Price8 (int64, x10^-8) | R |  |  |
| 20 | 2 | lastTradeDate | 9325 | LocalMktDateOptional (uint16) | O | 0 |  |
| 22 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 24 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 32 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 19 AuctionImbalance_19

blockLength **32**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDEntryType(269) = IMBALANCE = 'A'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | mDUpdateAction | 279 | MDUpdateAction enum (uint8) | R |  |  |
| 10 | 2 | imbalanceCondition | 37277 | ImbalanceCondition set (uint16) | R |  |  |
| 12 | 8 | mDEntrySize | 271 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 20 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 28 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 21 QuantityBand_21

blockLength **40**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = QUANTITY_BAND = 'h'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 3 | *padding* | | | | | |
| 12 | 8 | avgDailyTradedQty | 37003 | QuantityVolumeOptional (int64) | O | -9223372036854775808 |  |
| 20 | 8 | maxTradeVol | 1140 | QuantityVolumeOptional (int64) | O | -9223372036854775808 |  |
| 28 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 36 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 22 PriceBand_22

blockLength **48**. messageLength = 12 + blockLength. (since v8)

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = PRICE_BAND = 'g'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | priceBandType | 6939 | PriceBandType enum (uint8) | O | 255 |  |
| 10 | 1 | priceLimitType | 1306 | PriceLimitType enum (uint8) | O | 255 |  |
| 11 | 1 | priceBandMidpointPriceType | 37008 | PriceBandMidpointPriceType enum (uint8) | O | 255 |  |
| 12 | 8 | lowLimitPrice | 1148 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 |  |
| 20 | 8 | highLimitPrice | 1149 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 |  |
| 28 | 8 | tradingReferencePrice | 1150 | Fixed8 (int64, x10^-8) | O | -9223372036854775808 |  |
| 36 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 44 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 24 HighPrice_24

blockLength **32**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDEntryType(269) = SESSION_HIGH_PRICE = '7'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | mDUpdateAction | 279 | MDUpdateAction enum (uint8) | R |  |  |
| 10 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 28 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 25 LowPrice_25

blockLength **32**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDEntryType(269) = SESSION_LOW_PRICE = '8'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | mDUpdateAction | 279 | MDUpdateAction enum (uint8) | R |  |  |
| 10 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 28 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 27 LastTradePrice_27

blockLength **68**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = TRADE = '2'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | tradingSessionID | 336 | TradingSessionID enum (uint8) | R |  |  |
| 10 | 2 | tradeCondition | 277 | TradeCondition set (uint16) | R |  |  |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 28 | 4 | tradeID | 1003 | TradeID (uint32) | R |  |  |
| 32 | 4 | mDEntryBuyer | 288 | FirmOptional (uint32) | O | 0 |  |
| 36 | 4 | mDEntrySeller | 289 | FirmOptional (uint32) | O | 0 |  |
| 40 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 42 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 50 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |
| 54 | 2 | sellerDays | 287 | UInt16NULL (uint16) | O | 0 |  |
| 56 | 8 | mDEntryInterestRate | 37014 | Percentage (int64, x10^-4) | O | 0 |  |
| 64 | 1 | trdSubType | 829 | TrdSubType enum (uint8) | O | 0 | since v7 |
| 65 | 3 | *padding* | | | | | |


### 28 SettlementPrice_28

blockLength **40**. messageLength = 12 + blockLength. (since v9)

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = SETTLEMENT_PRICE = '6'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | *padding* | | | | | |
| 10 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 28 | 1 | openCloseSettlFlag | 286 | OpenCloseSettlFlag enum (uint8) | O | 255 | deprecated v17 |
| 29 | 1 | priceType | 423 | PriceType enum (uint8) | O | 0 |  |
| 30 | 1 | settlPriceType | 731 | SettlPriceType enum (uint8) | R |  |  |
| 31 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |
| 35 | 2 | settlDate | 64 | LocalMktDate (uint16) | O | 65535 | since v17 |
| 37 | 3 | *padding* | | | | | |


### 29 OpenInterest_29

blockLength **32**. messageLength = 12 + blockLength. (since v9)

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = OPEN_INTEREST = 'C'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | *padding* | | | | | |
| 10 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 12 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 20 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 28 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 30 SnapshotFullRefresh_Header_30

blockLength **34**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataSnapshotFullRefresh = 'W', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 4 | lastMsgSeqNumProcessed | 369 | SeqNum (uint32) | R |  |  |
| 12 | 4 | totNumReports | 911 | UInt32 (uint32) | R |  |  |
| 16 | 4 | totNumBids | 37071 | UInt32 (uint32) | R |  |  |
| 20 | 4 | totNumOffers | 37072 | UInt32 (uint32) | R |  |  |
| 24 | 2 | totNumStats | 37070 | UInt16 (uint16) | R |  |  |
| 26 | 2 | *padding* | | | | | |
| 28 | 4 | lastRptSeq | 37083 | RptSeq (uint32) | O | 0 |  |
| 32 | 2 | lastSequenceVersion | 37084 | UInt16NULL (uint16) | O | 0 | since v15 |


### 50 Order_MBO_50

blockLength **72**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | mDUpdateAction | 279 | MDUpdateAction enum (uint8) | R |  |  |
| 10 | 1 | mDEntryType | 269 | MDEntryType enum (char) | R |  |  |
| 11 | 1 | *padding* | | | | | |
| 12 | 8 | mDEntryPx | 270 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 |  |
| 20 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 28 | 4 | *padding* | | | | | |
| 32 | 4 | enteringFirm | 37501 | FirmOptional (uint32) | O | 0 |  |
| 36 | 8 | mDInsertTimestamp | 37034 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 44 | 8 | secondaryOrderID | 198 | OrderID (uint64) | R |  |  |
| 52 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |
| 56 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 | since v15 |
| 64 | 8 | mDEntryPrevSize | 37780 | QuantityOptional (int64) | O | -9223372036854775808 | since v16 |


### 51 DeleteOrder_MBO_51

blockLength **52**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = DELETE = 2

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | *padding* | | | | | |
| 10 | 1 | mDEntryType | 269 | MDEntryType enum (char) | R |  |  |
| 11 | 5 | *padding* | | | | | |
| 16 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 24 | 8 | secondaryOrderID | 198 | OrderID (uint64) | R |  |  |
| 32 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 | since v15 |
| 40 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |
| 44 | 8 | mDEntryPx | 270 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 | since v15 |


### 52 MassDeleteOrders_MBO_52

blockLength **28**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | mDUpdateAction | 279 | MDUpdateAction enum (uint8) | R |  |  |
| 10 | 1 | mDEntryType | 269 | MDEntryType enum (char) | R |  |  |
| 11 | 5 | *padding* | | | | | |
| 16 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 | since v15 |
| 24 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 53 Trade_53

blockLength **56**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = TRADE = '2'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | tradingSessionID | 336 | TradingSessionID enum (uint8) | R |  |  |
| 10 | 2 | tradeCondition | 277 | TradeCondition set (uint16) | R |  |  |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 28 | 4 | tradeID | 1003 | TradeID (uint32) | R |  |  |
| 32 | 4 | mDEntryBuyer | 288 | FirmOptional (uint32) | O | 0 |  |
| 36 | 4 | mDEntrySeller | 289 | FirmOptional (uint32) | O | 0 |  |
| 40 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 42 | 1 | trdSubType | 829 | TrdSubType enum (uint8) | O | 0 | since v7 |
| 43 | 1 | *padding* | | | | | |
| 44 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 | since v15 |
| 52 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 54 ForwardTrade_54

blockLength **68**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = TRADE = '2'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | tradingSessionID | 336 | TradingSessionID enum (uint8) | R |  |  |
| 10 | 2 | tradeCondition | 277 | TradeCondition set (uint16) | R |  |  |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 28 | 4 | tradeID | 1003 | TradeID (uint32) | R |  |  |
| 32 | 4 | mDEntryBuyer | 288 | FirmOptional (uint32) | O | 0 |  |
| 36 | 4 | mDEntrySeller | 289 | FirmOptional (uint32) | O | 0 |  |
| 40 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 42 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 | since v15 |
| 50 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |
| 54 | 2 | sellerDays | 287 | UInt16NULL (uint16) | O | 0 |  |
| 56 | 8 | mDEntryInterestRate | 37014 | Percentage (int64, x10^-4) | O | 0 |  |
| 64 | 1 | trdSubType | 829 | TrdSubType enum (uint8) | O | 0 | since v7 |
| 65 | 3 | *padding* | | | | | |


### 55 ExecutionSummary_55

blockLength **64**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = EXECUTION_SUMMARY = 's'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 2 | *padding* | | | | | |
| 10 | 1 | aggressorSide | 2446 | AggressorSide enum (uint8) | R |  |  |
| 11 | 1 | *padding* | | | | | |
| 12 | 8 | lastPx | 31 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | fillQty | 1365 | Quantity (int64) | R |  |  |
| 28 | 8 | tradedHiddenQty | 37779 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 36 | 8 | cxlQty | 84 | QuantityOptional (int64) | O | -9223372036854775808 |  |
| 44 | 8 | aggressorTime | 2445 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 52 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |
| 56 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 | since v15 |


### 56 ExecutionStatistics_56

blockLength **52**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = NEW = 0, mDEntryType(269) = EXECUTION_STATISTICS = '9'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | tradingSessionID | 336 | TradingSessionID enum (uint8) | R |  |  |
| 10 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 12 | 8 | tradeVolume | 1020 | QuantityVolume (int64) | R |  |  |
| 20 | 8 | vwapPx | 37778 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 |  |
| 28 | 8 | netChgPrevDay | 451 | PriceOffset8Optional (int64, x10^-8) | O | -9223372036854775808 |  |
| 36 | 4 | numberOfTrades | 37073 | NumberOfTrades (uint32) | R |  |  |
| 40 | 8 | mDEntryTimestamp | 37033 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 48 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 57 TradeBust_57

blockLength **48**. messageLength = 12 + blockLength.

Constants (0 bytes on the wire): messageType(35) = MarketDataIncrementalRefresh = 'X', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF, mDUpdateAction(279) = DELETE = 2, mDEntryType(269) = TRADE_BUST = 'u'

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |
| 8 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  |  |
| 9 | 1 | tradingSessionID | 336 | TradingSessionID enum (uint8) | R |  |  |
| 10 | 2 | *padding* | | | | | |
| 12 | 8 | mDEntryPx | 270 | Price (int64, x10^-4) | R |  |  |
| 20 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 28 | 4 | tradeID | 1003 | TradeID (uint32) | R |  |  |
| 32 | 2 | tradeDate | 75 | LocalMktDate (uint16) | R |  |  |
| 34 | 2 | *padding* | | | | | |
| 36 | 8 | transactTime | 60 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 | since v15 |
| 44 | 4 | rptSeq | 83 | RptSeq (uint32) | O | 0 |  |


### 71 SnapshotFullRefresh_Orders_MBO_71

blockLength **8** + groups/var data. messageLength = 12 + blockLength + groups/var data.

Constants (0 bytes on the wire): messageType(35) = MarketDataSnapshotFullRefresh = 'W', applVerID(1128) = FIX50SP2 = 9, securityIDSource(22) = EXCHANGE_SYMBOL = '8', securityExchange(207) = BVMF

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | securityID | 48 | SecurityID (uint64) | R |  |  |

Group **noMDEntries** (268), dimension GroupSizeEncoding (3 B: blockLength uint16, numInGroup uint8), entry blockLength **42**. Offsets relative to each entry.

| Off | Size | Field | Tag | Type | P | Null | Notes |
|---|---|---|---|---|---|---|---|
| 0 | 8 | mDEntryPx | 270 | PriceOptional (int64, x10^-4) | O | -9223372036854775808 |  |
| 8 | 8 | mDEntrySize | 271 | Quantity (int64) | R |  |  |
| 16 | 4 | *padding* | | | | | |
| 20 | 4 | enteringFirm | 37501 | FirmOptional (uint32) | O | 0 |  |
| 24 | 8 | mDInsertTimestamp | 37034 | UTCTimestampNanos (uint64 ns since epoch) | R | 0 |  |
| 32 | 8 | secondaryOrderID | 198 | OrderID (uint64) | R |  |  |
| 40 | 1 | mDEntryType | 269 | MDEntryType enum (char) | R |  |  |
| 41 | 1 | matchEventIndicator | 37035 | MatchEventIndicator set (uint8) | R |  | since v10 |


---

## 8. Composite types

| Composite | Size | Members |
|---|---|---|
| Price | 8 | mantissa int64 @0; exponent int8 = -4 (constant) |
| PriceOptional | 8 | mantissa int64 @0; exponent int8 = -4 (constant) |
| Percentage | 8 | mantissa int64 @0, null 0; exponent int8 = -4 (constant) |
| RatioQty | 8 | mantissa int64 @0; exponent int8 = -7 (constant) |
| UTCTimestampNanos | 8 | time uint64 @0, null 0; unit uint8 = TimeUnit.NANOSECOND (constant) |
| UTCTimestampSeconds | 8 | time int64 @0; unit uint8 = TimeUnit.SECOND (constant) |
| MaturityMonthYear | 5 | year uint16 @0, null 0; month uint8 @2, null 0; day uint8 @3, null 0; week uint8 @4, null 0 |
| Fixed8 | 8 | mantissa int64 @0; exponent int8 = -8 (constant) |
| Price8 | 8 | mantissa int64 @0; exponent int8 = -8 (constant) |
| PriceOffset8Optional | 8 | mantissa int64 @0; exponent int8 = -8 (constant) |

## 9. Simple types

Optional types without an explicit null use the SBE default null (int64 min for int64, 255 for uint8, and so on).

| Type | Encoding | Presence | Null |
|---|---|---|---|
| Int8 | int8 | required |  |
| Int16 | int16 | required |  |
| Int32 | int32 | required |  |
| Int64 | int64 | required |  |
| UInt8 | uint8 | required |  |
| UInt16 | uint16 | required |  |
| UInt32 | uint32 | required |  |
| UInt64 | uint64 | required |  |
| Int8NULL | int8 | optional | 0 |
| Int16NULL | int16 | optional | 0 |
| Int32NULL | int32 | optional | 0 |
| Int64NULL | int64 | optional | 0 |
| CharNULL | char | optional | 0 |
| UInt8NULL | uint8 | optional | 0 |
| UInt16NULL | uint16 | optional | 0 |
| UInt32NULL | uint32 | optional | 0 |
| UInt64NULL | uint64 | optional | 0 |
| NumberOfTrades | uint32 | required |  |
| Quantity | int64 | required |  |
| QuantityOptional | int64 | optional |  |
| QuantityVolume | int64 | required |  |
| QuantityVolumeOptional | int64 | optional |  |
| FirmOptional | uint32 | optional | 0 |
| OrderID | uint64 | required |  |
| TradeID | uint32 | required |  |
| SeqNum | uint32 | required |  |
| SecurityID | uint64 | required |  |
| SecurityIDOptional | uint64 | optional | 0 |
| SecurityExchange | char[4] | required |  |
| RptSeq | uint32 | optional | 0 |
| Symbol | char[20] | required |  |
| ISINNumber | char[12] | required |  |
| ClearingHouseID | uint64 | optional | 0 |
| NewsID | uint64 | optional | 0 |
| Currency | char[3] | required |  |
| SecurityStrategyType | char[3] | optional |  |
| Asset | char[6] | required |  |
| SettlType | uint16 | required |  |
| CFICode | char[6] | required |  |
| CountryCode | char[2] | required |  |
| LanguageCode | char[2] | required |  |
| LocalMktDate | uint16 | required |  |
| LocalMktDateOptional | uint16 | optional | 0 |
| LocalMktDate32 | int32 | required |  |
| LocalMktDate32Optional | int32 | optional | 0 |
| SecurityGroup | char[3] | required |  |
| MarketSegmentID | uint8 | optional | 0 |

## 10. Enums and sets

A set is a bitfield: each listed bit number is a flag.

| Name | Encoding | Values |
|---|---|---|
| MessageType | enum char | Sequence = '0', SequenceReset = '4', MarketDataIncrementalRefresh = 'X', SecurityStatus = 'f', SecurityDefinition = 'd', News = 'B', MarketDataSnapshotFullRefresh = 'W' |
| Boolean | enum uint8 | FALSE_VALUE = 0, TRUE_VALUE = 1 |
| TimeUnit | enum uint8 | SECOND = 0, MILLISECOND = 3, MICROSECOND = 6, NANOSECOND = 9 |
| Side | enum uint8 | BUY = 1, SELL = 2 |
| SecurityUpdateAction | enum char | ADD = 'A', DELETE = 'D', MODIFY = 'M' |
| LotType | enum uint8 | ODD_LOT = 1, ROUND_LOT = 2, BLOCK_LOT = 3 |
| Product | enum uint8 | COMMODITY = 2, CORPORATE = 3, CURRENCY = 4, EQUITY = 5, GOVERNMENT = 6, INDEX = 7, ECONOMIC_INDICATOR = 15, MULTILEG = 16 |
| SecurityType | enum uint8 | CASH = 1, CORP = 2, CS = 3, DTERM = 4, ETF = 5, FOPT = 6, FORWARD = 7, FUT = 8, INDEX = 9, INDEXOPT = 10, MLEG = 11, OPT = 12, OPTEXER = 13, PS = 14, SECLOAN = 15, SOPT = 16, SPOT = 17 |
| ExerciseStyle | enum uint8 | EUROPEAN = 0, AMERICAN = 1 |
| PutOrCall | enum uint8 | PUT = 0, CALL = 1 |
| PriceType | enum UInt8NULL; null 0 | PERCENTAGE = 1, PU = 2, FIXED_AMOUNT = 3 |
| SecurityTradingStatus | enum uint8 | PAUSE = 2, CLOSE = 4, OPEN = 17, FORBIDDEN = 18, UNKNOWN_OR_INVALID = 20, RESERVED = 21, FINAL_CLOSING_CALL = 101 |
| TradingSessionSubID | enum uint8 | PAUSE = 2, CLOSE = 4, OPEN = 17, PRE_CLOSE = 18, UNKNOWN_OR_INVALID = 20, PRE_OPEN = 21, FINAL_CLOSING_CALL = 101 |
| GovernanceIndicator | enum uint8 | No = 0, N1 = 1, N2 = 2, NM = 4, MA = 5, MB = 6, M2 = 7 |
| SecurityMatchType | enum uint8 | ISSUING_BUY_BACK_AUCTION = 8 |
| AggressorSide | enum uint8 | NO_AGGRESSOR = 0, BUY = 1, SELL = 2 |
| TradingSessionID | enum uint8 | REGULAR_TRADING_SESSION = 1, NON_REGULAR_TRADING_SESSION = 6 |
| SecurityTradingEvent | enum uint8 | TRADING_SESSION_CHANGE = 4, SECURITY_STATUS_CHANGE = 101, SECURITY_REJOINS_SECURITY_GROUP_STATUS = 102 |
| PriceBandType | enum uint8 | HARD_LIMIT = 1, AUCTION_LIMITS = 2, REJECTION_BAND = 3, STATIC_LIMITS = 4 |
| OpenCloseSettlFlag | enum uint8 | DAILY = 0, SESSION = 1, EXPECTED_ENTRY = 3, ENTRY_FROM_PREVIOUS_BUSINESS_DAY = 4, THEORETICAL_PRICE = 5 |
| PriceLimitType | enum uint8 | PRICE_UNIT = 0, TICKS = 1, PERCENTAGE = 2 |
| PriceBandMidpointPriceType | enum uint8 | LAST_TRADED_PRICE = 0, COMPLEMENTARY_LAST_PRICE = 1, THEORETICAL_PRICE = 2 |
| SettlPriceType | enum uint8 | FINAL = 1, THEORETICAL = 2, UPDATED = 3 |
| UnderlyingPxType | enum uint8 | TRADE = 0, AVERAGE_OF_TOB = 1 |
| MDUpdateAction | enum uint8 | NEW = 0, CHANGE = 1, DELETE = 2, DELETE_THRU = 3, DELETE_FROM = 4, OVERLAY = 5 |
| MDEntryType | enum char | BID = '0', OFFER = '1', TRADE = '2', INDEX_VALUE = '3', OPENING_PRICE = '4', CLOSING_PRICE = '5', SETTLEMENT_PRICE = '6', SESSION_HIGH_PRICE = '7', SESSION_LOW_PRICE = '8', EXECUTION_STATISTICS = '9', IMBALANCE = 'A', TRADE_VOLUME = 'B', OPEN_INTEREST = 'C', EMPTY_BOOK = 'J', SECURITY_TRADING_STATE_PHASE = 'c', PRICE_BAND = 'g', QUANTITY_BAND = 'h', COMPOSITE_UNDERLYING_PRICE = 'D', EXECUTION_SUMMARY = 's', VOLATILITY_PRICE = 'v', TRADE_BUST = 'u' |
| NewsSource | enum uint8 | OTHER = 0, DCM = 1, BBMNET = 2, MARKET_SURVEILLANCE = 3, INTERNET = 4, DPR_VE = 5, MKT_OPS_FX_AGENCY = 19, MKT_OPS_DERIVATIVES_AGENCY = 20, OVER_THE_COUNTER_NEWS_AGENCY = 11, ELECTRONIC_PURCHASE_EXCHANGE = 13, CBLC_NEWS_AGENCY = 14, BOVESPA_INDEX_AGENCY = 15, BOVESPA_INSTITUTIONAL_AGENCY = 16, MKT_OPS_EQUITIES_AGENCY = 17, BOVESPA_COMPANIES_AGENCY = 18 |
| ApplVerID | enum uint8 | FIX27 = 0, FIX30 = 1, FIX40 = 2, FIX41 = 3, FIX42 = 4, FIX43 = 5, FIX44 = 6, FIX50 = 7, FIX50SP1 = 8, FIX50SP2 = 9 |
| MultiLegModel | enum uint8 | PREDEFINED = 0, USER_DEFINED = 1 |
| MultiLegPriceMethod | enum uint8 | NET_PRICE = 0, REVERSED_NET_PRICE = 1, YIELD_DIFFERENCE = 2, INDIVIDUAL = 3, CONTRACT_WEIGHTED_AVERAGE_PRICE = 4, MULTIPLIED_PRICE = 5 |
| InstrAttribType | enum uint8 | TRADE_TYPE_ELIGIBILITY = 24, GTD_GTC_ELIGIBILITY = 34, TEST_INSTRUMENT = 35 (since v17) |
| InstrAttribValue | enum uint8 | ELECTRONIC_MATCH_OR_GTD_GTC_ELIGIBLE = 1, ORDER_CROSS_ELIGIBLE = 2, BLOCK_TRADE_ELIGIBLE = 3, FLAG_RFQ_FOR_CROSS_ELIGIBLE = 14, NEGOTIATED_QUOTE_ELIGIBLE = 17 |
| SecurityIDSource | enum char | ISIN = '4', EXCHANGE_SYMBOL = '8' |
| TrdSubType | enum UInt8NULL; null 0 | MULTI_ASSET_TRADE = 101, LEG_TRADE = 102, MIDPOINT_TRADE = 103, BLOCK_BOOK_TRADE = 104, RF_TRADE = 105 (since v8), RLP_TRADE = 106, TAC_TRADE = 107, TAA_TRADE = 108, SWEEP_TRADE = 109 (since v9) |
| ImpliedMarketIndicator | enum uint8 | NOT_IMPLIED = 0, IMPLIED = 1 |
| OptPayoutType | enum UInt8NULL; null 0 | VANILLA = 1, VANILLA_CASH_SETTLEMENT = 2 (since v17), BINARY = 3 |
| ImbalanceCondition | set uint16 | bit 8 ImbalanceMoreBuyers, bit 9 ImbalanceMoreSellers |
| TradeCondition | set uint16 | bit 0 OpeningPrice, bit 1 Crossed, bit 2 LastTradeAtTheSamePrice, bit 3 OutOfSequence, bit 6 TradeOnBehalf, bit 13 RegularTrade, bit 14 BlockTrade |
| MatchEventIndicator | set uint8 | bit 4 Implied (since v10), bit 5 RecoveryMsg, bit 7 EndOfEvent |

---

## 11. Feed architecture (Guidelines 6)

* A **channel** is a group of instruments with common characteristics. Each channel has three streams on separate multicast address/port pairs:
  * **Incremental**: real-time book updates, trades, statistics, status, news, definition updates.
  * **Snapshot recovery**: loops over the current state of every instrument.
  * **Instrument definition**: loops over every instrument's SecurityDefinition.
* **Feed A** carries all three streams. **Feed B** carries only the incremental stream, with exactly the same packets as Feed A over a different network path. Clients listen to both and keep whichever copy of each packet arrives first (dedupe by sequenceNumber). **Feed C** is the disaster-recovery site.
* Multicast addresses and ports come from B3's "Market Data Channels Definition" document. They are not in these specs.
* UDP gives no delivery or ordering guarantee. Packets can be lost or arrive out of order.

## 12. Feed behavior

### 12.1 Sequencing and heartbeats

* `sequenceNumber` increases by exactly 1 per packet within a channel and sequenceVersion. A jump means lost packets.
* When the incremental stream is idle for more than 1 s, B3 sends a packet containing a **Sequence** message. Its packet `sequenceNumber` is **0 and must be ignored**. Its `nextSeqNo` field is the sequenceNumber the next real packet will have.
* No message on a stream for 3 heartbeat intervals means the stream is down and the book should be treated as inconsistent.
* **SequenceReset** on the incremental stream means a severe failure or a regular start-up. The sequence restarts at 1 and sequenceVersion is incremented. After it, B3 replenishes the books with EmptyBook and Order_MBO messages that have the RecoveryMsg bit set.

### 12.2 Events and MatchEventIndicator

* Every order action in the matching engine is one **event**, which can produce one or many messages and span several packets.
* The **last message of an event has bit 7 (EndOfEvent, 0x80)** set in matchEventIndicator.
* Statistics, bands, group phases and statuses that do not change a book are not part of an event, and **each of those messages has EndOfEvent set on its own**.
* A mass cancel is one event across all affected orders, even across instruments. Only the last DeleteOrder has EndOfEvent.
* Bit 5 (RecoveryMsg, 0x20) is used only on Order_MBO, ChannelReset and EmptyBook messages sent during a recovery. Values seen: 0, 0x80, 0x20, 0xA0.
* Bit 4 (Implied, 0x10) marks implied (synthetic) orders and trades involving them.
* Order rejections and stop order entries/cancels produce no market data.

### 12.3 Order book management (MBO, price/priority)

* The book is full depth, order by order, per instrument and side.
* Bids are sorted by price high to low, offers low to high. Within a price, orders are sorted by **secondaryOrderID ascending** (smaller is higher priority). Orders with a null price (market-on-auction/close) form the best level on either side.
* An order is identified by **(securityID, side, price, secondaryOrderID)**. secondaryOrderID changes whenever the order loses priority, changes price, or replenishes a disclosed (iceberg) quantity. It is unique across segments and only grows. Implied orders get larger values than ordinary ones.

| Message | Book action |
|---|---|
| Order_MBO, mDUpdateAction NEW (0) | Insert the order |
| Order_MBO, mDUpdateAction CHANGE (1) | Change the size of an existing order **that keeps its priority** (same secondaryOrderID). `mDEntryPrevSize` carries the previous size, which also allows maintaining a by-price book |
| DeleteOrder_MBO | Remove the order. `mDEntrySize` is its last remaining size, `mDEntryPx` its price |
| MassDeleteOrders_MBO, DELETE_THRU (3) | Remove every order on the given side. Statistics are kept |
| EmptyBook | Remove all orders on both sides of one instrument. The instrument's rptSeq restarts at 1 with the next message |
| ChannelReset | Remove all orders of every instrument in the channel |

* A modification that **loses priority** (size increase, price change) is published as **DeleteOrder_MBO of the old order followed by Order_MBO NEW** with a new secondaryOrderID.
* After a match, a resting order that was fully filled gets a **DeleteOrder_MBO**, and a partially filled one gets an **Order_MBO CHANGE** with the reduced size.
* **Trade messages must not be used to update the book.** Book changes always come as Order/Delete messages.

### 12.4 Matching event structure

From the examples in Guidelines 10 and 13.1:

* **ExecutionSummary** is the first message of a matching event caused by an aggressor order, a cross, an RLP trade, or a block trade. It has no matchEventIndicator field because it is never the last message. It carries aggressorSide, lastPx (last fill price), fillQty, tradedHiddenQty and cxlQty.
* Then come the **Trade** messages (one per fill), the **book updates** for the passive orders (DeleteOrder / Order CHANGE), any **statistics** (OpeningPrice, HighPrice, LowPrice, ...), and an **ExecutionStatistics** message.
* The **last message of the event has EndOfEvent** set. In B3's examples that is ExecutionStatistics.
* Self-trade-prevention cancels of resting orders are published as DeleteOrder_MBO before the trades.
* B3's own examples do not use one consistent order between trades, book updates and statistics. A consumer should not depend on that order, only on ExecutionSummary first and EndOfEvent last.
* Auction-uncross trades, manual trades by market operations, and forward ("termo") trades do not produce an ExecutionSummary.

### 12.5 rptSeq

* `rptSeq` is a per-instrument sequence number in almost every incremental message that has a securityID.
* A gap in rptSeq for one instrument means a message for that instrument was lost, even if packet sequencing looks fine for the instruments you care about.
* It restarts at 1 after an EmptyBook for that instrument. It is zero in the snapshot stream.

### 12.6 Snapshot recovery stream

Each loop contains, in order:

1. SecurityGroupPhase for each security group of the channel.
2. For each instrument with data:
   1. SnapshotFullRefresh_Header (lastMsgSeqNumProcessed, lastSequenceVersion, totNumReports, totNumBids, totNumOffers, totNumStats, lastRptSeq).
   2. One or more SnapshotFullRefresh_Orders_MBO (about 21 orders each). This is omitted if both sides are empty.
   3. Statistics and status messages for that instrument (OpeningPrice, ClosingPrice, LastTradePrice, ExecutionStatistics, SecurityStatus, bands, ...).
3. A final packet containing **only SequenceReset**. Its sequenceNumber equals the number of packets in the loop.

Other rules:

* The snapshot is valid as of incremental packet `lastMsgSeqNumProcessed` in sequenceVersion `lastSequenceVersion`.
* `totNumReports` is the number of instruments in the loop, which may be fewer than the instruments in the channel. Missing instruments have empty books.
* Trade, ForwardTrade, TradeBust, News, SecurityDefinition and ExecutionSummary are never sent on the snapshot stream.

### 12.7 Instrument definition stream

* Loops over one SecurityDefinition per instrument, starting at sequenceNumber 1. `totNoRelatedSym` is the count for the loop.
* Each loop ends with SequenceReset. The next loop has a new sequenceVersion and starts at sequenceNumber 1.
* Deleted or expired instruments only appear as SecurityDefinition on the incremental stream.
* Both PUMA segments (Equities, Derivatives/FX) stay up all week and are brought down between Friday 22:00 and Sunday 12:00 local time.

### 12.8 Initial synchronization (late join)

1. Join the incremental stream and queue packets.
2. Join the instrument definition stream until `totNoRelatedSym` definitions are received, then leave it.
3. Join the snapshot stream until a complete loop is received (`totNumReports` instruments, no packet gaps, final SequenceReset), then leave it.
4. Apply the queued incrementals in order, discarding those with sequenceNumber at or below each instrument's `lastMsgSeqNumProcessed` (same sequenceVersion). Do not discard SecurityDefinition or News.
5. Continue with live incrementals.

### 12.9 Channel reset and book reset

* **ChannelReset** (component failure or system start): clear every book in the channel. If it happens at system start, the instrument list is resent (the last SecurityDefinition has lastFragment = true). Then, per instrument that had a book: EmptyBook, followed by the Order_MBO messages that rebuild it, all with RecoveryMsg set. Once an instrument is rebuilt, its live updates resume without RecoveryMsg, while other instruments may still be recovering.
* Do not clear statistics until a SecurityGroupPhase with securityTradingEvent = TRADING_SESSION_CHANGE (4) arrives.
* **EmptyBook** alone (single instrument) follows the same pattern for one instrument.

### 12.10 Large messages

No message exceeds one packet. Messages that could be larger (News) are split into parts with `partCount` and `partNumber` (1 to partCount), and the client concatenates the text.

---

## 13. Known errors and inconsistencies in B3's documents

The XML wins in every case below.

| Document | Issue |
|---|---|
| Message Reference, LastTradePrice_27 | Lists an extra row `mDInsertTimestamp` at offset 68 (8), "since schema version 16", but states encoded length 68, and the schema 17 XML has no such field. If B3 ever adds it, it will show up as a larger blockLength in the header, which a decoder following section 5 handles automatically |
| Message Reference 9.2.18 and Guidelines 13.11.3, SettlementPrice_28 | List `priceType` as R (required). In the XML the field has no presence attribute and its enum `PriceType` is encoded as `UInt8NULL` (optional, null 0), so the field is optional with null 0. B3 deliberately changed this enum's null to 0 (schema changelog 1.5.5). Decoders should treat 0 as null |
| Message Reference, PriceBand_22 | Summary table names it "PriceBand_20" (template 20 was removed in schema 10). Template id 22 and length 48 are correct |
| Message Reference, sample packet (6.3/6.5) | Table shows SchemaVersion hex as `10 00` while the hex dump and decoded value are `11 00` = 17. The dump is right |
| Guidelines 6.5.5.4 and 6.5.5.5 | Example packets use schemaVersion 3, Order blockLength 60, Trade blockLength 52. These are from an old schema and do not match version 17 |
| Guidelines 10 | Example table gives old message sizes (Trade 52, OpeningPrice 40, DeleteOrder 40) and lists ExecutionStatistics as template 55. Current values: Trade 56, OpeningPrice 44, DeleteOrder 52, ExecutionStatistics 56 |
