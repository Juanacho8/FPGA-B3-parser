"""Record layouts of docs/DESIGN.md sections 2 to 4."""

import struct

import pytest

import b3contract as c


def test_sizes():
    assert c.FILE_HEADER.size == 32
    assert c.EVENT.size == 64
    assert c.BOOK_HEADER.size == 32
    assert c.LEVEL.size == 24


@pytest.mark.parametrize("n,size", [(1, 128), (2, 128), (5, 320), (10, 512), (16, 832)])
def test_book_record_size(n, size):
    assert c.book_record_size(n) == size
    assert c.book_record_size(n) % 64 == 0
    assert c.book_record_size(n) >= 32 + 48 * n


def test_event_field_offsets():
    e = c.Event(seq_num=0x11223344, seq_version=0x5566, channel=0x77, msg_index=0x88,
                kind=0x99, flags=0xAA, side=0xBB, venue_msg_type=0xCC, rpt_seq=0xDDEEFF00,
                instrument_id=1, price=-2, qty=3, order_id=4, exch_time=5, aux=-1)
    b = e.pack()
    assert b[0:4] == bytes.fromhex("44332211")
    assert b[4:6] == bytes.fromhex("6655")
    assert b[6:12] == bytes.fromhex("778899aabbcc")
    assert struct.unpack_from("<I", b, 12)[0] == 0xDDEEFF00
    assert struct.unpack_from("<QqqQQQ", b, 16) == (1, -2, 3, 4, 5, 0xFFFF_FFFF_FFFF_FFFF)


def test_null_price_stays_int64_min():
    b = c.Event(0, 0, 0, 0, c.DELETE, price=c.INT64_NULL).pack()
    assert b[24:32] == bytes.fromhex("0000000000000080")


def test_header_round_trip():
    h = c.book_header(5, 0, 64)
    assert h.record_size == 320 and h.tracked_depth == 0
    assert c.FileHeader.unpack(h.pack()) == h


def test_read_events_rejects_bad_files():
    good = c.events_header().pack() + c.Event(1, 1, 68, 0, c.ADD).pack()
    header, events = c.read_events(good)
    assert len(events) == 1 and events[0].kind == c.ADD
    with pytest.raises(ValueError):
        c.read_events(good[:-1])                      # partial record
    with pytest.raises(ValueError):
        c.read_events(b"XXXXXXXX" + good[8:])         # magic
    with pytest.raises(ValueError):
        c.read_events(c.book_header().pack())         # a book.bin


def test_kind_values_are_fixed():
    assert [c.ADD, c.MODIFY, c.DELETE, c.CLEAR_SIDE, c.CLEAR_BOOK, c.CLEAR_CHANNEL,
            c.EXEC_SUMMARY, c.TRADE, c.TRADE_BUST, c.SESSION, c.SEQ_RESET,
            c.END_OF_EVENT] == list(range(1, 13))
