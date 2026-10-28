"""docs/DESIGN.md 4.7's port 2 example must match tools/expected_book.py."""

import b3contract as c
import book_example as ex
import expected_book as eb
from test_design_examples import design_example


def test_book_records_example():
    port2 = eb.run_packets(ex.packets(), ex.TIMES, ex.TABLE, ex.MANIFEST)
    assert design_example("book_records") == b"".join(port2.records)
    assert [r[40] for r in port2.records] == [1, 2, 3, 2]          # one of each type
    assert [r[45] for r in port2.records] == [0, c.CAUSE_GAP, 0, c.CAUSE_EMPTY_BOOK]   # cause: STATE_CHANGE only
    assert [int.from_bytes(r[:8], "little") for r in port2.records] == [1, 2, 3, 4]


def test_record_size_and_header():
    assert c.book_record_size(5) == 320 and c.book_record_size(1) == 128 and c.book_record_size(10) == 576
    h = eb.file_header(5, 100_000_000)
    assert h[:8] == b"B3PORT2\x00" and int.from_bytes(h[12:16], "little") == 320
    assert int.from_bytes(h[18:20], "little") == 0                  # K = 0: uncapped
    assert int.from_bytes(h[24:32], "little") == 100_000_000        # the STATUS interval
