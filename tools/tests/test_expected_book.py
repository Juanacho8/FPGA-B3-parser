"""expected_book (the port 2 answer key) on hand-given packets, and book_compare's checks."""

import b3contract as c
import book_compare as bc
import expected_book as eb
from book_example import order
from rows import row

WIN, DOL = 200000001, 200000002
TABLE = [(WIN, 68), (DOL, 68)]
CLEAN_MANIFEST = {"missing": {"A": [], "B": []}}


def dele(sid, side, px, qty, soid, rpt, mei, seq, index):
    return row(51, {"securityID": sid, "matchEventIndicator": mei, "mDEntryType": side, "mDEntrySize": qty,
                    "secondaryOrderID": soid, "transactTime": 9, "rptSeq": rpt, "mDEntryPx": px},
               seq=seq, index=index)


def header(rec):
    return c.BOOK_HEADER.unpack_from(rec, 0)


def test_mass_cancel_one_update_per_instrument_in_first_touch_order():
    o1 = order(1, 0, "0", 100, 5, 1, 1, 0x00, 1)
    o2 = order(1, 1, "1", 200, 3, 2, 1, 0x80, 1)
    o2["fields"]["securityID"] = DOL
    pkts = [[o1, o2], [dele(DOL, "1", 200, 3, 2, 2, 0x00, 2, 0), dele(WIN, "0", 100, 5, 1, 2, 0x80, 2, 1)]]
    p = eb.run_packets(pkts, [0, 0], TABLE, CLEAN_MANIFEST, status_interval=0)
    sids = [header(r)[5] for r in p.records]
    assert sids == [WIN, DOL, DOL, WIN]
    assert [header(r)[8] for r in p.records] == [c.BOOK_UPDATE] * 4


def test_stale_publishes_nothing_unless_publish_stale():
    pkts = [[order(1, 0, "0", 100, 5, 1, 1, 0x80, 1)], [order(3, 0, "0", 101, 1, 2, 3, 0x80, 1)]]
    manifest = {"missing": {"A": [[68, 1, 2]], "B": []}}
    p = eb.run_packets(pkts, [0, 0], TABLE[:1], manifest, status_interval=0)
    assert [(header(r)[8], header(r)[9]) for r in p.records] == [(c.BOOK_UPDATE, c.BOOK_CLEAN),
                                                                  (c.STATE_CHANGE, c.BOOK_STALE)]
    p = eb.run_packets(pkts, [0, 0], TABLE[:1], manifest, status_interval=0, publish_stale=True)
    assert [(header(r)[8], header(r)[9]) for r in p.records] == [
        (c.BOOK_UPDATE, c.BOOK_CLEAN), (c.STATE_CHANGE, c.BOOK_STALE), (c.BOOK_UPDATE, c.BOOK_STALE)]


def test_state_change_cause_byte():
    # gap, EmptyBook, rptSeq jump, ChannelReset, then 10 s of silence.
    pkts = [[order(1, 0, "0", 100, 5, 1, 1, 0x80, 1)], [order(3, 0, "0", 101, 1, 2, 3, 0x80, 1)],
            [row(9, {"securityID": WIN, "matchEventIndicator": 0xA0, "mDEntryTimestamp": 3}, seq=4)],
            [order(5, 0, "0", 102, 1, 3, 5, 0x80, 1)],
            [row(11, {"matchEventIndicator": 0xA0, "mDEntryTimestamp": 4}, seq=6)],
            [order(7, 0, "0", 103, 1, 4, 1, 0x80, 1)]]
    manifest = {"missing": {"A": [[68, 1, 2]], "B": []}}
    p = eb.run_packets(pkts, [0, 1, 2, 3, 4, 10_000_000_004], TABLE[:1], manifest, status_interval=0)
    changes = [(header(r)[9], header(r)[13]) for r in p.records if header(r)[8] == c.STATE_CHANGE]
    assert changes == [(c.BOOK_STALE, c.CAUSE_GAP), (c.BOOK_CLEAN, c.CAUSE_EMPTY_BOOK), (c.BOOK_STALE, c.CAUSE_RPT_SEQ_JUMP),
                       (c.BOOK_CLEAN, c.CAUSE_CHANNEL_RESET), (c.BOOK_STALE, c.CAUSE_SILENCE)]
    assert all(header(r)[13] == 0 for r in p.records if header(r)[8] != c.STATE_CHANGE)


def test_status_ticks_and_out_seq():
    pkts = [[order(n, 0, "0", 100, 1, n, n, 0x80, 1)] for n in (1, 2, 3)]
    p = eb.run_packets(pkts, [1000, 1099, 1350], TABLE[:1], CLEAN_MANIFEST, status_interval=100)
    # At 1350 the ticks 1100, 1200 and 1300 are due: one STATUS, the latest,
    # with ticks_skipped 2 (offset 24).
    assert [header(r)[8] for r in p.records] == [1, 1, 3, 1]
    assert [header(r)[0] for r in p.records] == list(range(1, 5))
    status = [header(r) for r in p.records if header(r)[8] == c.STATUS]
    assert [(h[7], h[6]) for h in status] == [(1300, 2)]


def test_level_book_consistency_split():
    b = eb.LevelBook(cap=2)
    b.add(0, eb.BID, 100, 5, False)
    b.remove(0, eb.BID, 99, 1, False)
    b.change(0, eb.BID, 98, 1, 2, True)
    b.add(0, eb.BID, 101, 1, False)
    b.add(0, eb.BID, 102, 1, False)
    assert (b.errors, b.errors_stale, b.level_overflow) == (1, 1, 1)
    assert b.top(0, eb.BID, 5) == [(101, 1, 1), (100, 5, 1)]


def test_book_key_check_catches_a_wrong_level():
    plan = [((68, 1, 5, 0), WIN, [(100, 5, 1), (99, 1, 1)], [])]
    good = {(68, 1, 5, 0): {WIN: ("CLEAN", 0, [(100, 5, 1), (99, 1, 1)], [])}}
    bad = {(68, 1, 5, 0): {WIN: ("CLEAN", 0, [(100, 4, 1), (99, 1, 1)], [])}}
    stale = {(68, 1, 5, 0): {WIN: ("STALE", 0, [(100, 4, 1)], [])}}
    incomplete = {(68, 1, 5, 0): {WIN: ("CLEAN", 1, [(100, 4, 1)], [])}}   # bid side depth incomplete
    assert bc.check_key(plan, good) == (1, 0, 0, [])
    assert bc.check_key(plan, bad)[3]
    assert bc.check_key(plan, stale) == (0, 0, 0, [])
    assert bc.check_key(plan, {}) == (0, 1, 0, [])
    assert bc.check_key(plan, incomplete) == (1, 0, 1, [])
    assert bc.check_key(plan, {(68, 1, 5, 0): {WIN: ("CLEAN", 0, [(100, 5, 1)], [])}}, n=1) == (1, 0, 0, [])


def test_parse_dump():
    dump = bc.parse_dump("68 1 5 0 200000001 CLEAN 2 2 100 5 1 99 3 2 0\n")
    assert dump == {(68, 1, 5, 0): {WIN: ("CLEAN", 2, [(100, 5, 1), (99, 3, 2)], [])}}


# ---- The capped book (DESIGN.md 4.2), K = 4, N = 2 ----
def capped(k=4, n=2):
    return eb.CappedBook(k, n)


def prices(b, side=eb.BID):
    return [lv[0] for lv in b.top(0, side, 99)]


def test_capped_fill_then_truncate_and_ignore_worse_than_b():
    b = capped()
    for p in (100, 98, 99, 97):
        b.add(0, eb.BID, p, 1, False)
    assert prices(b) == [100, 99, 98, 97] and not b.sides[(0, eb.BID)]["truncated"]
    b.add(0, eb.BID, 96, 1, False)                         # K + 1: the new one is the worst, dropped
    st = b.sides[(0, eb.BID)]
    assert prices(b) == [100, 99, 98, 97] and st["truncated"] and st["b"] == 97
    b.add(0, eb.BID, 95, 1, False)                         # worse than B: ignored
    assert prices(b) == [100, 99, 98, 97] and b.stats[eb.BID]["ignored"] == 1
    b.add(0, eb.BID, 101, 1, False)                        # better: inserted, 97 pushed out
    assert prices(b) == [101, 100, 99, 98] and st["b"] == 98
    assert b.stats[eb.BID]["truncations"] == 1 and b.stats[eb.BID]["levels_dropped"] == 2


def test_capped_delete_keeps_b_then_new_at_b_and_depth_incomplete():
    b = capped()
    for p in (100, 99, 98, 97, 96):
        b.add(0, eb.BID, p, 1, False)
    st = b.sides[(0, eb.BID)]
    assert st["b"] == 97
    b.remove(0, eb.BID, 97, 1, False)                      # B unchanged
    assert prices(b) == [100, 99, 98] and st["b"] == 97
    b.add(0, eb.BID, 97, 2, False)                         # at B: inserted
    assert prices(b) == [100, 99, 98, 97]
    b.set_time(10)
    for p in (100, 99, 98):                                # a sweep: three levels go
        b.remove(0, eb.BID, p, 1, False)
    assert prices(b) == [97] and b.depth_incomplete(0, eb.BID)     # truncated, 1 < N
    b.set_time(25)
    b.add(0, eb.BID, 99, 1, False)                         # valid reaches N: complete again
    assert not b.depth_incomplete(0, eb.BID)
    assert b.stats[eb.BID]["depth_incomplete"] == 1 and b.stats[eb.BID]["depth_incomplete_ns"] == 15
    b.remove(0, eb.BID, 99, 1, False)
    assert b.depth_incomplete(0, eb.BID)
    b.clear(0, eb.BID)                                     # a clear ends it too
    assert not b.depth_incomplete(0, eb.BID) and not b.sides[(0, eb.BID)]["truncated"]
    assert b.errors == 0


def test_capped_consistency_error_better_than_b_and_mirror_offers():
    b = capped()
    for p in (100, 99, 98, 97, 96):
        b.add(0, eb.BID, p, 1, False)
    b.remove(0, eb.BID, 99, 1, False)
    b.remove(0, eb.BID, 99, 1, False)                      # better than B, not there: error
    b.remove(0, eb.BID, 90, 1, False)                      # worse than B: ignored
    assert b.errors == 1 and b.stats[eb.BID]["ignored"] == 1
    for p in (100, 102, 101, 103, 104):
        b.add(0, eb.OFFER, p, 1, False)
    assert prices(b, eb.OFFER) == [100, 101, 102, 103] and b.sides[(0, eb.OFFER)]["b"] == 103


def test_capped_mode_flags_depth_incomplete_on_port2():
    # K = 2, N = 2: three bids truncate; a delete leaves 1 < N valid while truncated.
    o = [order(1, i, "0", 100 - i, 1, i + 1, i + 1, 0x80 if i == 2 else 0, 1) for i in range(3)]
    d = dele(WIN, "0", 100, 1, 1, 4, 0x80, 2, 0)
    p = eb.run_packets([o, [d]], [0, 0], TABLE[:1], CLEAN_MANIFEST, n=2, status_interval=0, k=2)
    assert [header(r)[10] for r in p.records] == [0, c.BOOK_FLAG_DEPTH_INCOMPLETE_BID]
    assert [header(r)[11] for r in p.records] == [2, 1]
    assert eb.book_bytes(p, 0)[18:20] == (2).to_bytes(2, "little")      # K in the file header
