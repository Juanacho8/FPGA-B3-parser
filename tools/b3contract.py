"""b3contract: the two output contracts of docs/DESIGN.md, in Python.

What it does:
  Defines the byte layouts shared by the C++ baseline and the RTL: the
  32-byte file header of events.bin and book.bin, the 64-byte port 1 event
  record, and the port 2 book update record. Packs and unpacks them.
  docs/DESIGN.md is the specification; this module is one reading of it.

What it must not do:
  Import anything from b3gen (feed_generator). The tools are an independent
  implementation of the contract, so a b3gen bug cannot hide in them.
"""

import struct
from dataclasses import dataclass

# ---- 1. File header (both files, 32 bytes) ----
FILE_HEADER = struct.Struct("<8sHHIHHIQ")
assert FILE_HEADER.size == 32
EVENTS_MAGIC = b"B3PORT1\x00"
BOOK_MAGIC = b"B3PORT2\x00"
FORMAT_VERSION = 1

# Book parameters the files record (cpp_baseline/include/b3/book_params.hpp)
DEFAULT_PUBLISHED_DEPTH = 5
DEFAULT_TRACKED_DEPTH = 16
DEFAULT_MAX_INSTRUMENTS = 64


@dataclass(frozen=True)
class FileHeader:
    magic: bytes
    format_version: int
    header_size: int
    record_size: int
    published_depth: int     # N
    tracked_depth: int       # K, 0 = uncapped
    max_instruments: int

    def pack(self):
        return FILE_HEADER.pack(self.magic, self.format_version, self.header_size,
                                self.record_size, self.published_depth,
                                self.tracked_depth, self.max_instruments, 0)

    @classmethod
    def unpack(cls, data):
        if len(data) < FILE_HEADER.size:
            raise ValueError(f"file shorter than its {FILE_HEADER.size}-byte header")
        magic, ver, hsize, rsize, n, k, m, _ = FILE_HEADER.unpack_from(data, 0)
        if magic not in (EVENTS_MAGIC, BOOK_MAGIC):
            raise ValueError(f"bad magic {magic!r}")
        if ver != FORMAT_VERSION:
            raise ValueError(f"unsupported format version {ver}")
        return cls(magic, ver, hsize, rsize, n, k, m)


# ---- 2. Port 1: event record (64 bytes) ----
EVENT = struct.Struct("<IHBBBBBBIQqqQQQ")
assert EVENT.size == 64

# Kinds (DESIGN.md section 3.2). 0 is never written.
ADD, MODIFY, DELETE, CLEAR_SIDE, CLEAR_BOOK, CLEAR_CHANNEL = 1, 2, 3, 4, 5, 6
EXEC_SUMMARY, TRADE, TRADE_BUST, SESSION, SEQ_RESET, END_OF_EVENT = 7, 8, 9, 10, 11, 12
KIND_NAMES = {ADD: "ADD", MODIFY: "MODIFY", DELETE: "DELETE", CLEAR_SIDE: "CLEAR_SIDE",
              CLEAR_BOOK: "CLEAR_BOOK", CLEAR_CHANNEL: "CLEAR_CHANNEL",
              EXEC_SUMMARY: "EXEC_SUMMARY", TRADE: "TRADE", TRADE_BUST: "TRADE_BUST",
              SESSION: "SESSION", SEQ_RESET: "SEQ_RESET", END_OF_EVENT: "END_OF_EVENT"}

# Flags (byte 9)
FLAG_END_OF_EVENT = 0x01
FLAG_RECOVERY = 0x02
FLAG_IMPLIED = 0x04

# Side (byte 10)
SIDE_NONE, SIDE_BID, SIDE_OFFER = 0, 1, 2

INT64_NULL = -(1 << 63)


@dataclass(frozen=True)
class Event:
    seq_num: int
    seq_version: int
    channel: int
    msg_index: int
    kind: int
    flags: int = 0
    side: int = 0
    venue_msg_type: int = 0
    rpt_seq: int = 0
    instrument_id: int = 0
    price: int = 0           # int64
    qty: int = 0             # int64
    order_id: int = 0        # uint64
    exch_time: int = 0       # uint64
    aux: int = 0             # uint64 (signed values stored as two's complement)

    def pack(self):
        return EVENT.pack(self.seq_num, self.seq_version, self.channel, self.msg_index,
                          self.kind, self.flags, self.side, self.venue_msg_type,
                          self.rpt_seq, self.instrument_id, self.price, self.qty,
                          self.order_id, self.exch_time, self.aux & 0xFFFF_FFFF_FFFF_FFFF)

    @classmethod
    def unpack_from(cls, data, offset=0):
        return cls(*EVENT.unpack_from(data, offset))


def events_header(published_depth=DEFAULT_PUBLISHED_DEPTH,
                  tracked_depth=DEFAULT_TRACKED_DEPTH,
                  max_instruments=DEFAULT_MAX_INSTRUMENTS):
    return FileHeader(EVENTS_MAGIC, FORMAT_VERSION, FILE_HEADER.size, EVENT.size,
                      published_depth, tracked_depth, max_instruments)


# ---- 3. Port 2: book update record (32-byte header + 2 x N levels, padded to 64) ----
BOOK_HEADER = struct.Struct("<IHBBQQBBB5x")
assert BOOK_HEADER.size == 32
LEVEL = struct.Struct("<qqI4x")
assert LEVEL.size == 24

BOOK_FLAG_STALE = 0x01
BOOK_FLAG_RECOVERY = 0x02
BOOK_FLAG_DEPTH_INCOMPLETE_BID = 0x04
BOOK_FLAG_DEPTH_INCOMPLETE_OFFER = 0x08


def book_record_size(published_depth):
    raw = BOOK_HEADER.size + 2 * published_depth * LEVEL.size
    return (raw + 63) // 64 * 64


def book_header(published_depth=DEFAULT_PUBLISHED_DEPTH,
                tracked_depth=DEFAULT_TRACKED_DEPTH,
                max_instruments=DEFAULT_MAX_INSTRUMENTS):
    return FileHeader(BOOK_MAGIC, FORMAT_VERSION, FILE_HEADER.size,
                      book_record_size(published_depth), published_depth,
                      tracked_depth, max_instruments)


# ---- 4. Reading a whole events.bin ----
def read_events(data):
    """(FileHeader, [Event]) from the bytes of an events.bin. The layout comes
    from the header, never assumed."""
    header = FileHeader.unpack(data)
    if header.magic != EVENTS_MAGIC:
        raise ValueError(f"not an events.bin (magic {header.magic!r})")
    if header.record_size != EVENT.size:
        raise ValueError(f"record size {header.record_size}, this reader knows {EVENT.size}")
    body = len(data) - header.header_size
    if body % header.record_size:
        raise ValueError(f"{body} bytes after the header is not a whole number of records")
    events = [Event.unpack_from(data, off)
              for off in range(header.header_size, len(data), header.record_size)]
    return header, events
