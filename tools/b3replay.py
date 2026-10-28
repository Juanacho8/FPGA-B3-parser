"""b3replay: the decoder's sequencing and stale rules, replayed over the answer keys.

What it does:
  Replay runs a feed's view of intent.jsonl (tools/b3intent.py) packet by
  packet through the rules of docs/DESIGN.md 3.6.3 (sequencing) and 3.6.4
  (rptSeq and stale flags), from the manifest's missing packets and faults:
  a gap or a discarded malformed tail marks every table instrument of the
  channel stale, an rptSeq jump one instrument, EmptyBook and ChannelReset
  clear. With capture times (tools/b3feed.py drives advance() and saw() from
  the feed's pcap), a channel silent for more than the silence limit marks
  its table instruments stale too (DESIGN.md 3.6.4, spec/LAYOUTS.md 12.1). It is the one derivation of stale state shared by
  tools/counters_compare.py (counters, first stale points) and
  tools/intent_to_events.py (port 1's "book not clean" flag).
  Every gap it finds must be packets the manifest lists as missing.

What it must not do:
  Import b3gen, or read any C++ output.
"""

from pathlib import Path

SEQUENCE_RESET, SEQUENCE, EMPTY_BOOK, CHANNEL_RESET = 1, 2, 9, 11
MAX_VERSION = 65534
DEFAULT_SILENCE_NS = 3_000_000_000   # 3 heartbeat intervals of 1 s (LAYOUTS.md 12.1)


class Broken(Exception):
    """The answer keys cannot give an expected value; never guessed."""


def read_table(path):
    """[(securityID, channel)] from an instrument table file."""
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].split()
        if line:
            out.append((int(line[0]), int(line[1])))
    return out


def default_table():
    """[(securityID, channel)] of the feed generator's config.toml, as
    tools/gen_instruments.py writes it."""
    import gen_instruments
    lines = gen_instruments.table_text(gen_instruments.REPO / "feed_generator" / "config.toml")
    out = []
    for line in lines.splitlines():
        line = line.split("#", 1)[0].split()
        if line:
            out.append((int(line[0]), int(line[1])))
    return out


def packets(rows):
    """Group a feed's rows into packets (each starts at index 0)."""
    current = []
    for row in rows:
        if row["index"] == 0 and current:
            yield current
            current = []
        current.append(row)
    if current:
        yield current


class Replay:
    """The sequencer and tracker rules over the answer keys' rows."""

    def __init__(self, table, manifest, feed, silence_ns=DEFAULT_SILENCE_NS):
        self.table = table
        self.silence_ns = silence_ns
        self.last_seen = {}   # channel -> capture time of its last datagram
        self.silent = set()   # channels already marked for their current silence
        self.silences = 0
        self.slot = {sid: i for i, (sid, _) in enumerate(table)}
        self.missing = {tuple(k) for k in manifest["missing"][feed]}
        self.faulted = {(f["channel"], f["sequenceVersion"], f["sequenceNumber"])
                        for f in manifest.get("faults") or [] if f["fault"] != "short"}
        self.ch = {}   # channel -> {"seen", "reset", "version", "next"}
        self.seq = dict(sequenced=0, gaps=0, gap_events=0, heartbeats=0, heartbeat_mismatches=0,
                        resets=0, reset_mismatches=0, version_changes=0, seq_reset_not_first=0,
                        heartbeat_without_sequence=0,
                        late_after_gap=0)   # the view never holds a late packet: only 0 is derivable
        self.msg_before_rebuild = 0
        self.losses = 0
        self.unknown = 0
        self.state = [dict(have=False, last=0, stale=False, first=None, gaps=0, events=0, rebuild=False)
                      for _ in table]
        self.gap_packets = set()

    # ---- silence (capture time) ----
    def advance(self, t):
        """Time reaches t (a datagram arrives): each channel that has sent
        nothing for more than the silence limit marks its table instruments
        stale, once per silence, at (channel, version, next expected number)."""
        if not self.silence_ns:
            return
        for ch, last in self.last_seen.items():
            if ch not in self.silent and t - last > self.silence_ns:
                self.silent.add(ch)
                self.silences += 1
                c = self.ch.get(ch, {"version": 0, "next": 0})
                point = (ch, c["version"], c["next"])
                for slot, (_, tch) in enumerate(self.table):
                    if tch == ch:
                        self.stale(slot, point)

    def saw(self, ch, t):
        """A datagram of channel ch at t (any datagram with a packet header)."""
        self.last_seen[ch] = t
        self.silent.discard(ch)

    # ---- sequencer ----
    def packet(self, rows, on_row=None):
        """One packet. on_row(row), if given, runs after each message's rules,
        so it sees the state the message left (DESIGN.md 3.3 bit 3)."""
        first = rows[0]
        ch, ver, num = first["channel"], first["sequenceVersion"], first["sequenceNumber"]
        c = self.ch.setdefault(ch, dict(seen=False, reset=False, version=0, next=0))
        if num == 0:
            self.seq["heartbeats"] += 1
            if first["templateId"] != SEQUENCE:
                self.seq["heartbeat_without_sequence"] += 1
        else:
            self.seq["sequenced"] += 1
            if first["templateId"] == SEQUENCE_RESET:
                self.seq["resets"] += 1
                c["reset"] = True
            else:
                self.sequence(c, ch, ver, num)
        for row in rows:
            self.message(row, c)
            if on_row:
                on_row(row)
        if (ch, ver, num) in self.faulted:
            self.loss(ch, (ch, ver, num))   # discarded malformed tail, after its messages

    def sequence(self, c, ch, ver, num):
        if c["reset"]:
            c["reset"] = False
            want = ver if not c["seen"] else (1 if c["version"] >= MAX_VERSION else c["version"] + 1)
            if (ver, num) != (want, 1):
                self.seq["reset_mismatches"] += 1
            c.update(seen=True, version=want, next=1)
        if not c["seen"]:
            c.update(seen=True, version=ver, next=num + 1)
        elif ver != c["version"]:
            self.seq["version_changes"] += 1
            c.update(version=ver, next=num + 1)
        elif num == c["next"]:
            c["next"] += 1
        elif num > c["next"]:
            lost = {(ch, ver, n) for n in range(c["next"], num)}
            if not lost <= self.missing:
                raise Broken(f"gap {sorted(lost - self.missing)} is not in the manifest's missing list")
            self.gap_packets |= lost
            self.seq["gaps"] += num - c["next"]
            self.seq["gap_events"] += 1
            self.loss(ch, (ch, ver, c["next"]))
            c["next"] = num + 1
        else:
            raise Broken(f"duplicate packet {(ch, ver, num)} in a single feed's intent view")

    # ---- tracker ----
    def loss(self, ch, point):
        self.losses += 1
        for slot, (_, tch) in enumerate(self.table):
            if tch == ch:
                self.stale(slot, point)

    def stale(self, slot, point):
        st = self.state[slot]
        if not st["stale"]:
            st["events"] += 1
        st["stale"] = True
        if st["first"] is None:
            st["first"] = list(point)

    def message(self, row, c):
        t, f = row["templateId"], row["fields"]
        ch = row["channel"]
        if t == SEQUENCE and row["sequenceNumber"] == 0:
            if c["seen"] or c["reset"]:
                expected = 1 if c["reset"] else c["next"]
                if f.get("nextSeqNo") != expected:
                    self.seq["heartbeat_mismatches"] += 1
        elif t == SEQUENCE_RESET and row["index"] > 0:
            self.seq["seq_reset_not_first"] += 1
        elif t == CHANNEL_RESET:
            for slot, (_, tch) in enumerate(self.table):
                if tch == ch:
                    self.state[slot].update(stale=False, rebuild=True)
        sid = f.get("securityID")
        if sid is None or "securityID" in row["null"]:
            return
        slot = self.slot.get(sid)
        if slot is None:
            self.unknown += 1
            return
        st = self.state[slot]
        if t == EMPTY_BOOK:
            st.update(stale=False, have=True, last=0, rebuild=False)
            return
        if st["rebuild"]:
            self.msg_before_rebuild += 1   # after a ChannelReset, before its EmptyBook (open item J)
        if "rptSeq" in f and "rptSeq" not in row.get("absent", []):
            if st["have"] and f["rptSeq"] != st["last"] + 1:
                st["gaps"] += 1
                self.stale(slot, (ch, row["sequenceVersion"], row["sequenceNumber"]))
            st.update(have=True, last=f["rptSeq"])


    def is_stale(self, security_id):
        """True when security_id is a table instrument and stale now."""
        slot = self.slot.get(security_id)
        return slot is not None and self.state[slot]["stale"]
