"""book_oracle's comparison: what may differ between the capped and uncapped books."""

import b3contract as c
import book_oracle as bo

N = 2


def file(k, *recs):
    return c.book_header(N, k, 64, 100).pack() + b"".join(r.pack(N) for r in recs)


def rec(flags=0, bids=(), offers=(), **kw):
    return c.BookRecord(1, c.BOOK_UPDATE, 5, 1, 68, 0, 200000001, flags=flags, bids=bids, offers=offers, **kw)


def test_only_k_flags_and_flagged_side_may_differ():
    full = ((100, 1, 1), (99, 1, 1))
    capped = rec(c.BOOK_FLAG_DEPTH_INCOMPLETE_BID, bids=((100, 1, 1),), offers=((101, 2, 1),))
    uncapped = rec(0, bids=full, offers=((101, 2, 1),))
    problems, records, flagged = bo.compare(file(3, capped), file(0, uncapped))
    assert problems == [] and records == 1 and flagged == 1


def test_an_unflagged_side_must_match():
    capped = rec(c.BOOK_FLAG_DEPTH_INCOMPLETE_BID, offers=((101, 2, 1),))
    uncapped = rec(0, offers=((101, 3, 1),))
    assert bo.compare(file(3, capped), file(0, uncapped))[0]


def test_other_differences_and_headers_are_bugs():
    assert bo.compare(file(3, rec(cause=1)), file(0, rec()))[0]                  # any other byte
    assert bo.compare(file(3, rec()), file(3, rec()))[0]                         # uncapped K must be 0
    assert bo.compare(file(3, rec()), file(0, rec(), rec()))[0]                  # record count
    assert bo.compare(file(3, rec()), file(0, rec(c.BOOK_FLAG_DEPTH_INCOMPLETE_OFFER)))[0]   # uncapped flag
