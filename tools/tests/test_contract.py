"""Record layouts of docs/DESIGN.md sections 2 to 4."""

import struct

import pytest

import b3contract as c
from test_design_examples import design_example


def test_sizes():
    assert c.FILE_HEADER.size == 32
    assert c.EVENT.size == 64
    assert c.BOOK_HEADER.size == 48
    assert c.LEVEL.size == 24


@pytest.mark.parametrize("n,size", [(1, 128), (2, 192), (5, 320), (10, 576), (16, 832)])
def test_book_record_size(n, size):
    assert c.book_record_size(n) == size
    assert c.book_record_size(n) % 64 == 0
    assert c.book_record_size(n) >= 48 + 48 * n


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
    h = c.book_header(5, 0, 64, 100_000_000)
    assert h.record_size == 320 and h.tracked_depth == 0
    assert h.pack()[24:32] == (100_000_000).to_bytes(8, "little")   # the STATUS interval
    assert c.FileHeader.unpack(h.pack()) == h
    assert c.events_header().pack()[24:32] == bytes(8)


def test_book_layout_against_design_examples():
    """DESIGN.md 4.7's four records, read with this module's layout."""
    data = design_example("book_records")
    size = c.book_record_size(5)
    assert len(data) == 4 * size
    recs = [c.BookRecord.unpack_from(data, i * size) for i in range(4)]
    win = 200000001
    assert recs[0] == c.BookRecord(1, c.BOOK_UPDATE, 1, 1, 68, 1, win, 999_000_100, 1_000_000_000,
                                   c.BOOK_CLEAN, 0, 0, ((1299800000, 5, 1), (1299750000, 3, 1)), ())
    assert recs[1] == c.BookRecord(2, c.STATE_CHANGE, 2, 1, 68, 0, win, 0, 1_050_000_000,
                                   c.BOOK_STALE, 0, c.CAUSE_GAP)
    assert recs[2] == c.BookRecord(3, c.STATUS, capture_time=1_100_000_000)
    assert recs[3] == c.BookRecord(4, c.STATE_CHANGE, 4, 1, 68, 0, win, 1_149_000_000, 1_150_000_000,
                                   c.BOOK_CLEAN, 0, c.CAUSE_EMPTY_BOOK)
    assert b"".join(r.pack(5) for r in recs) == data       # and back, padding included
    # Field offsets (DESIGN.md 4.1)
    r = data[size:2 * size]
    assert r[40] == c.STATE_CHANGE and r[41] == c.BOOK_STALE and r[45] == c.CAUSE_GAP and r[46:48] == bytes(2)


def test_book_constant_values_are_fixed():
    assert (c.BOOK_UPDATE, c.STATE_CHANGE, c.STATUS) == (1, 2, 3)
    assert (c.BOOK_CLEAN, c.BOOK_HOLDING, c.BOOK_STALE, c.BOOK_RECOVERING) == (0, 1, 2, 3)
    assert (c.BOOK_FLAG_RECOVERY, c.BOOK_FLAG_DEPTH_INCOMPLETE_BID, c.BOOK_FLAG_DEPTH_INCOMPLETE_OFFER) == (1, 2, 4)
    assert [c.CAUSE_GAP, c.CAUSE_DISCARDED_TAIL, c.CAUSE_RPT_SEQ_JUMP, c.CAUSE_SILENCE, c.CAUSE_EMPTY_BOOK,
            c.CAUSE_CHANNEL_RESET] == list(range(1, 7))


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
