"""The port 2 example of docs/DESIGN.md 4.7: one record of each type.

Channel 68, WIN (securityID 200000001) the only table instrument.
  packet 1, capture 1.000 s: two Order_MBO NEW bids, the second closes the
            event                                   -> BOOK_UPDATE
  packet 3, capture 1.050 s: packet 2 is missing    -> STATE_CHANGE to STALE
            an Order_MBO NEW offer (STALE: no BOOK_UPDATE)
  packet 4, capture 1.150 s: the 1.100 s tick       -> STATUS
            WIN's EmptyBook                         -> STATE_CHANGE to CLEAN
"""

from rows import row

WIN = 200000001
TABLE = [(WIN, 68)]
MANIFEST = {"missing": {"A": [[68, 1, 2]], "B": []}}
TIMES = [1_000_000_000, 1_050_000_000, 1_150_000_000]


def order(seq, index, side, px, qty, soid, rpt, mei, time):
    return row(50, {"securityID": WIN, "matchEventIndicator": mei, "mDUpdateAction": 0, "mDEntryType": side,
                    "mDEntryPx": px, "mDEntrySize": qty, "enteringFirm": 15, "mDInsertTimestamp": time,
                    "secondaryOrderID": soid, "rptSeq": rpt, "transactTime": time,
                    "mDEntryPrevSize": -(1 << 63)}, seq=seq, index=index)


def packets():
    return [
        [order(1, 0, "0", 1299800000, 5, 1, 1, 0x00, 999_000_000),
         order(1, 1, "0", 1299750000, 3, 2, 2, 0x80, 999_000_100)],
        [order(3, 0, "1", 1299850000, 2, 3, 4, 0x80, 1_049_000_000)],
        [row(9, {"securityID": WIN, "matchEventIndicator": 0x20, "mDEntryTimestamp": 1_149_000_000}, seq=4)],
    ]
