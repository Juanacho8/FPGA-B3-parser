"""dump_events prints one line per record."""

import b3contract as c
import dump_events
import intent_to_events as ie
from rows import trade_event_rows


def test_dump(tmp_path, capsys):
    path = tmp_path / "events.bin"
    path.write_bytes(c.events_header().pack()
                     + b"".join(e.pack() for e in ie.convert(trade_event_rows())))
    assert dump_events.main([str(path), "--hex"]) == 0
    out = capsys.readouterr().out
    assert "4 records of 64 B, N=5 K=16" in out
    assert "EXEC_SUMMARY" in out and "END_OF_EVENT" in out and "EOE" in out
    assert "10 00 00 00 01 00 46 03 0c 01 00 38" in out
