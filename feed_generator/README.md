# b3gen: B3 Binary UMDF feed generator

b3gen produces what B3's market data system would multicast on the
incremental stream: UDP packets in pcap files, for Feed A and Feed B. With
them come answer keys that say what every byte means. Use the files to test
the C++ baseline and the RTL parser: decode the pcap, then compare your
output with the keys.

Python 3.11+, standard library only.

## Run it

From `feed_generator/`:

```sh
python -m b3gen build                         # every set, from config.toml, into ./data
python -m b3gen build --out /tmp/feeds --sets small,scenarios
python -m b3gen build --sets scenarios --scenarios loss,gap,failover
python -m b3gen build --config my.toml --seed 7 --messages 5000
python -m pytest                              # the test suite
```

| Option | Meaning |
|---|---|
| `--config PATH` | TOML config (default `feed_generator/config.toml`) |
| `--out DIR` | output directory (default `data`, which git ignores) |
| `--sets LIST` | `small`, `large`, `burst`, `scenarios`, or `all` (default) |
| `--scenarios LIST` | which scenario folders, or `all` (default) |
| `--seed N` | overrides the config's seed |
| `--messages N` | message target for every set chosen (the `malformed` file always has 1,500) |

The same config and seed always give byte-identical files. If the config is
bad, or a scenario can't place what it is for within its message target, the
command prints one line starting `error:` and exits with status 2.

What the default config produces, measured on a laptop:

| Folder | Messages | Time | Peak memory | On disk |
|---|---|---|---|---|
| `small/` | 20,000 | ~1 s | ~40 MB | 12 MB |
| `large/` | 1,000,000 | ~46 s | ~0.6 GB | 0.6 GB |
| `burst/` | 200,000, a spike every 50 ms instead of every 1 s (~100 spikes) | ~8 s | ~0.1 GB | 128 MB |
| `scenarios/<name>/` | 6,000 each (malformed: 1,500) | ~4 s for all 15 | ~50 MB | |

Feed A and the intent log are streamed to disk as packets close. Most of
the large set's peak memory is Feed B, which is written at the end, sorted by
its own (delayed) times.

## Scenarios

| Folder | What is in it |
|---|---|
| `clean` | the plain session, both feeds |
| `loss` | Feed A misses bursts of packets that Feed B carries |
| `gap` | bursts of packets missing from both feeds |
| `gap_one` | one packet missing from both feeds, holding messages for one instrument only |
| `idle` | a 3.5 s pause with heartbeats on both feeds |
| `failover` | SequenceReset, sequenceVersion +1, every book rebuilt (EmptyBook + recovery orders) |
| `channel_reset` | ChannelReset on one channel, then its books rebuilt |
| `instrument_reset` | EmptyBook and a rebuild for one instrument |
| `session_change` | end of day, stream down overnight, start-of-day heartbeats, next session (statistics, tradeID and tradeDate reset; new price bands) |
| `weekly_start` | Friday, weekend down, start-of-day heartbeats, SequenceReset, ChannelReset, rebuilds, Monday's session |
| `edge` | a mass cancel of each side of one instrument, and a sweep whose event spans 3+ packets |
| `rare` | a TradeBust of a real trade, News in one part and in 3 parts with live traffic between the parts |
| `schema` | messages with a larger blockLength and version 18, and an unknown templateId |
| `malformed` | a short file with one fault per packet (bad messageLength, wrong encodingType, truncation) and a stray short datagram |

## Output files

Every folder has these six files:

| File | Contents |
|---|---|
| `feedA.pcap`, `feedB.pcap` | Ethernet/IPv4/UDP frames, nanosecond pcap (magic `0xa1b23c4d`). Feed B has the same packets byte for byte, 20 to 30 µs later, on its own multicast groups |
| `intent.jsonl` | one row per message on the wire: what the publisher meant |
| `book_key.jsonl` | the book a correct client holds at checkpoints |
| `manifest.json` | what the folder contains and where: missing packets, resets, injections, faults |
| `provenance.json` | how the folder was made |

### intent.jsonl

There is one JSON object per line, in wire order: packet order in Feed A,
then message index within the packet. Heartbeats are included.

```json
{"channel":68,"sequenceVersion":1,"sequenceNumber":2,"index":0,
 "sendingTime":1792414800001969967,"templateId":50,"blockLength":72,"version":17,
 "instrument":"WIN",
 "fields":{"securityID":200000001,"matchEventIndicator":128,"mDUpdateAction":0,
           "mDEntryType":"0","mDEntryPx":1299800000,"mDEntrySize":4,"enteringFirm":15,
           "mDInsertTimestamp":1792414800001964967,"secondaryOrderID":1,"rptSeq":6,
           "transactTime":1792414800001964967,"mDEntryPrevSize":-9223372036854775808},
 "null":["mDEntryPrevSize"]}
```

- `fields` holds every non-constant field with its wire value:
  - Prices are mantissas, ×10⁻⁴; ClosingPrice and netChgPrevDay are ×10⁻⁸.
  - Timestamps are nanoseconds since the epoch.
  - Chars are strings; a null char is `""`.
- `null` lists the optional fields whose wire value is their null.
- `blockLength` and `version` are the message header's values. They differ from the schema only in the `schema` scenario.
- An unknown template has `"fields": {"body": "<hex>"}`.
- The last part of a News message also carries `"news": {"newsID", "headline", "text"}`, with the text reassembled.

To check a decoder, decode each pcap message and compare it with the row at
the same `(channel, sequenceVersion, sequenceNumber, index)`.

### book_key.jsonl

Each line is one checkpoint, taken every `book_key_every` events, after
every rebuild, and after a new session starts:

```json
{"key":[68,1,547,0],
 "books":{"WIN":{"as_of":[68,1,547,0],
                 "bids":[[1299800000,9,4],[1299750000,26,14],...],
                 "offers":[[1299850000,42,16],[1299900000,31,11],...]},
          "DOL":{...}, "PETR4":{...}, "VALE3":{...}}}
```

- **Levels:** `bids` and `offers` are the top 5 levels, best first, each written as `[price, total quantity, number of orders]`.
- **When to compare:** replay the feed, and compare an instrument's book when its own channel reaches `as_of`. That is the placement of the last message published on that channel at checkpoint time; channels are packetized independently.
- **What the key shows:** the book a correct client holds, not the matching engine's book. The two differ between a ChannelReset and that instrument's rebuild, where the key shows an empty book.

### manifest.json

| Key | Meaning |
|---|---|
| `scenario`, `seed`, `target_messages`, `messages`, `heartbeats` | what was built |
| `params` | Feed B delay and jitter, loss/gap/idle parameters, the packets `gap_one` dropped |
| `missing.A`, `missing.B` | `[channel, sequenceVersion, sequenceNumber]` of each packet missing from that feed |
| `unreliable_books` | instrument → the first missing packet that breaks its book. Without a snapshot (v2), a client can't trust that book from there on. Only SecurityGroupPhase, ChannelReset and SequenceReset make a whole channel unreliable |
| `resets` | per reset: kind, time, target, first and last placement, each channel's SequenceReset placement, each instrument's rebuild span, trade dates and bands for a new session |
| `injections` | per edge/rare/schema action: kind, parameters, and the placements of its messages |
| `faults` (malformed) | per fault: packet, kind, byte offset and the number of complete messages before it |
| `expected_client_behavior` | for `schema` and `malformed`: what a correct client does |

**A/B arbitration:** take each `(channel, sequenceVersion, sequenceNumber)`
from whichever feed has it first. In the `loss` scenario the merged feed is
complete, so its book matches the book key everywhere. In `gap` it matches
except for the instruments in `unreliable_books`, which match only up to
their first missing packet.

### provenance.json

It records how the folder was made:
- the config: its path, SHA-256 and full text
- the effective seed and message target
- the set and scenario
- the generator's git commit, and whether the working tree had uncommitted changes
- the schema XML's SHA-256 (the one `layouts.py` was generated from, plus a fresh hash of `spec/*.xml`)
- the command line, the Python version and the start time

## Configuration

`config.toml` documents every value and labels each one:

| Label | Meaning |
|---|---|
| **[B3]** | a fact from B3's documents in `spec/` |
| **[B3 market]** | a real B3 convention, such as a contract's tick or lot |
| **[inferred]** | our reading where B3 is silent (open items in `docs/FEED_GENERATOR_CONTEXT.md`) |
| **[invented]** | made up for realism |
| **[stress]** | chosen to stress parsers |

Validation is strict. An unknown or missing key, a wrong type or an
inconsistent value fails with the key path, for example
`config.toml: instruments[2].tick: expected int, got str ('100')`.

## Where things are

| Path | What |
|---|---|
| `b3gen/` | the generator: flow → engine → publisher → packet → pcap, plus golden (answer keys), scenarios, config, cli |
| `tools/gen_layouts.py` | regenerates `b3gen/layouts.py` from B3's XML (SHA-256 checked) |
| `tests/` | the tests, which decode and rebuild books independently: nothing in `tests/` relies on b3gen to read the output |
| `../docs/FEED_GENERATOR_CONTEXT.md` | design, decisions and open items |
| `../spec/LAYOUTS.md` | the wire format and B3's feed rules |
