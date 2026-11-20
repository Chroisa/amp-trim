#!/usr/bin/env python3
"""
amp-trim – Clean and bound a script(1) typescript for use inside an
           animated man-page box (amp).

Reads a timing file + typescript, removes screen-clearing / absolute
cursor sequences that would fight the surrounding pager, and constrains
the visible content to a fixed width × height region while preserving
timing.

When --width and --height are omitted the tool only measures the content
and prints the minimal bounding box (width × height) that would contain
every character after cleaning.  Clear-screen sequences are treated as
frame boundaries, so height is the maximum height of any single frame
(not the sum of all frames).

-R / --shift can be used on its own (with -T/-S).  In that case the first
N columns are discarded and the remaining content is kept at its natural
size (no additional width/height clipping).

Vertical behaviour (height=H):
  Within a frame, a sliding window of the last H lines is kept.
  A clear-screen sequence starts a new frame (buffer is reset).

Horizontal behaviour (width=W, shift=S):
  Columns 0 .. S-1 are discarded (the “shift”).  The following W
  columns become the visible content.  Timing belonging to the shifted
  and truncated characters is retained.
  If only -R is given, W is effectively unlimited (detected width − shift).

Usage:
  # Measure only – print the minimal bounding box
  amp-trim -t in.tm -s in.ts

  # Shift left by 5 columns only (no width/height required)
  amp-trim -t in.tm -s in.ts -R 5 -T out.tm -S out.ts

  # Trim to an explicit box
  amp-trim -t in.tm -s in.ts -W 48 -H 10 [-R 2] \\
           -T out.tm -S out.ts

  # with detailed removal statistics / debug
  amp-trim ... --verbose --debug
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from typing import List, Tuple, Dict, Optional


# Frame boundaries: only erase-display sequences.
# Bare CUP home (H / 1;1H) is a cursor move, not a new frame – caca emits
# "\033[H\033[2J\033[3J" as a single redraw; treating H alone as a boundary
# produced alternating blank frames.
CLEAR_OR_HOME = re.compile(
    rb"""
    \x1b\[2J          # erase entire display
    | \x1b\[3J        # erase scrollback
    """,
    re.VERBOSE,
)

# Sequences always removed from the stream (CUP is handled in process()).
OTHER_DANGEROUS = re.compile(
    rb"\x1b[cC]|"                              # RIS
    rb"\x1b\[[\?0-9;]*[hl]|"                   # mode set/reset (?2004h etc.)
    rb"\x1b\[[0-9]*[ABCDEFG]|"                 # relative cursor (A–G)
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|"  # OSC … BEL/ST
    rb"\x1b\[\?[0-9;]*[a-zA-Z]|"              # private CSI
    rb"\x00"                                   # NUL
)

# SGR and other colour codes we keep
CSI_SGR_OR_SAFE = re.compile(r"\033\[[0-9;]*m")


class Stats:
    """Collect detailed information about what was found / removed."""

    def __init__(self):
        self.counts: Counter = Counter()
        self.examples: Dict[str, bytes] = {}

    def add(self, kind: str, sample: bytes = b""):
        self.counts[kind] += 1
        if kind not in self.examples and sample:
            self.examples[kind] = sample[:40]

    def report(self, file=sys.stderr):
        if not self.counts:
            print("amp-trim: no special sequences or truncations recorded", file=file)
            return
        print("amp-trim: detailed character / sequence report", file=file)
        print("-" * 50, file=file)
        for kind, n in sorted(self.counts.items(), key=lambda x: (-x[1], x[0])):
            sample = self.examples.get(kind, b"")
            extra = f"  e.g. {sample!r}" if sample else ""
            print(f"  {kind:30s} {n:6d}{extra}", file=file)
        print("-" * 50, file=file)


def parse_timing_typescript(timing_path: str, ts_path: str) -> List[Tuple[float, bytes]]:
    """Return list of (delay_seconds, data_bytes)."""
    events: List[Tuple[float, bytes]] = []
    with open(timing_path, "r", encoding="utf-8", errors="replace") as tf, \
         open(ts_path, "rb") as sf:
        for line in tf:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                delay = float(parts[0])
                count = int(parts[1])
            except ValueError:
                continue
            data = sf.read(count)
            if len(data) < count:
                break
            events.append((delay, data))
    return events


def visible_width(s: str) -> int:
    """Display columns: strip CSI/OSC, then count printable chars only."""
    s = re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", s)
    s = re.sub(r"\033\][^\007\033]*(?:\007|\033\\)", "", s)
    return sum(1 for ch in s if ch.isprintable() or ch == " ")


# script(1) writes a non-displayed header/footer line into the typescript.
# These must not affect the measured bounding box or the trimmed frames.
SCRIPT_META_RE = re.compile(
    r"^(Script started on |Script done on )"
)


def is_script_meta_line(s: str) -> bool:
    """True for script(1) header/footer lines (not shown by scriptreplay)."""
    # Strip CSI first so a coloured header still matches
    plain = re.sub(r"\033\[[0-9;]*[a-zA-Z]", "", s).lstrip()
    return bool(SCRIPT_META_RE.match(plain))


def split_clears(data: bytes) -> List[Tuple[bool, bytes]]:
    """
    Split a byte chunk into pieces, tagging clear/home sequences.
    Returns list of (is_clear, payload).
    """
    out: List[Tuple[bool, bytes]] = []
    pos = 0
    for m in CLEAR_OR_HOME.finditer(data):
        if m.start() > pos:
            out.append((False, data[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(data):
        out.append((False, data[pos:]))
    return out if out else [(False, data)]


def strip_dangerous(data: bytes, stats: Optional[Stats]) -> bytes:
    """Remove non-clear dangerous sequences; keep SGR colours."""
    def sub_and_count(pattern, kind, blob):
        def repl(m):
            if stats is not None:
                stats.add(kind, m.group(0))
            return b""
        return pattern.sub(repl, blob)

    data = sub_and_count(OTHER_DANGEROUS, "dangerous_ESC_cursor/reset", data)
    if b"\x0c" in data:
        n = data.count(b"\x0c")
        if stats is not None:
            stats.counts["form_feed"] += n
        data = data.replace(b"\x0c", b"")
    return data


def detect_bbox(events: List[Tuple[float, bytes]], stats: Optional[Stats]) -> Tuple[int, int]:
    """
    Walk the stream and return the minimal (width, height).

    Clear-screen / home sequences are treated as *frame boundaries*:
    the height of each frame is measured independently and the maximum
    is reported.  This avoids summing the heights of every animation
    frame in a caca-style recording.
    """
    max_width = 0
    max_height = 0

    frame_lines: List[str] = []
    current = ""
    saw_cr = False  # collapse CRLF into a single line ending

    def finish_line():
        nonlocal current, saw_cr
        # Drop script(1) header/footer – they are not part of the display
        if not is_script_meta_line(current):
            frame_lines.append(current)
        current = ""
        saw_cr = False

    def finish_frame():
        nonlocal frame_lines, current, max_width, max_height, saw_cr
        if current:
            finish_line()
        # Trailing empty line from a final newline is not real content
        while frame_lines and frame_lines[-1] == "":
            frame_lines.pop()
        h = len(frame_lines)
        max_height = max(max_height, h)
        for ln in frame_lines:
            max_width = max(max_width, visible_width(ln.rstrip("\r")))
        frame_lines = []
        current = ""
        saw_cr = False

    for delay, raw in events:
        for is_clear, piece in split_clears(raw):
            if is_clear:
                if stats is not None:
                    stats.add("frame_boundary_clear/home", piece)
                finish_frame()
                continue
            cleaned = strip_dangerous(piece, stats)
            try:
                text = cleaned.decode("utf-8", errors="replace")
            except Exception:
                text = cleaned.decode("latin1", errors="replace")
            for ch in text:
                if ch == "\r":
                    # End of visual line (common in ansi/caca art)
                    finish_line()
                    saw_cr = True
                elif ch == "\n":
                    # If this is the LF of a CRLF pair, ignore it
                    if saw_cr:
                        saw_cr = False
                    else:
                        finish_line()
                elif ch == "\b":
                    saw_cr = False
                    if current:
                        current = current[:-1]
                else:
                    saw_cr = False
                    current += ch

    finish_frame()
    return max(1, max_width), max(1, max_height)


class BoundedBuffer:
    """Maintain a height-H sliding window of lines, each of width W,
    after discarding the first `shift` columns.  Clear starts a new frame."""

    def __init__(self, width: int, height: int, shift: int = 0):
        self.width = max(1, width)
        self.height = max(1, height)
        self.shift = max(0, shift)
        self.lines: List[str] = [""]
        self.completed: List[str] = []

    def reset(self):
        """Start a new frame (after clear-screen)."""
        self.lines = [""]
        self.completed = []

    def _flush_line(self):
        raw = self.lines[-1]
        self.lines = [""]
        # Drop script(1) header/footer – not part of the displayed session
        if is_script_meta_line(raw):
            return
        if self.shift:
            raw = raw[self.shift:]
        # truncate by visible width approximately: strip CSI for measure,
        # but keep colour codes in the stored line up to width columns
        line = self._truncate_visible(raw, self.width)
        self.completed.append(line)
        if len(self.completed) > self.height:
            self.completed = self.completed[-self.height:]

    @staticmethod
    def _truncate_visible(s: str, width: int) -> str:
        """Keep the leading part of s whose visible width is <= width."""
        out = []
        vis = 0
        i = 0
        while i < len(s) and vis < width:
            if s[i] == "\033":
                # copy whole CSI sequence
                j = i + 1
                if j < len(s) and s[j] == "[":
                    j += 1
                    while j < len(s) and not s[j].isalpha():
                        j += 1
                    if j < len(s):
                        j += 1
                    out.append(s[i:j])
                    i = j
                    continue
            out.append(s[i])
            vis += 1
            i += 1
        return "".join(out)

    def feed(self, text: str, stats: Optional[Stats]):
        for ch in text:
            if ch == "\n":
                self._flush_line()
            elif ch == "\r":
                self.lines[-1] = ""
            elif ch == "\b":
                if self.lines[-1]:
                    self.lines[-1] = self.lines[-1][:-1]
            elif ch == "\x00" or (ord(ch) < 32 and ch not in "\t"):
                # drop NUL and other C0 controls (CSI already stripped)
                continue
            else:
                self.lines[-1] += ch
                if stats is not None:
                    col = visible_width(self.lines[-1]) - 1
                    if col < self.shift:
                        stats.add("shifted_left_of_box")
                    elif col >= self.shift + self.width:
                        stats.add("truncated_right_of_box")

    def visible(self) -> List[str]:
        buf = list(self.completed)
        cur = self.lines[-1]
        if self.shift:
            # shift by visible columns is hard; approximate by char shift on
            # stripped form – for coloured caca, shift is usually 0
            pass
        cur = self._truncate_visible(cur[self.shift:] if self.shift else cur, self.width)
        buf.append(cur)
        while len(buf) < self.height:
            buf.insert(0, "")
        buf = buf[-self.height:]
        # pad visible width
        padded = []
        for ln in buf:
            extra = self.width - visible_width(ln)
            if extra > 0:
                ln = ln + " " * extra
            padded.append(ln)
        return padded


def emit_frame_from_lines(lines: List[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8", errors="replace")


def emit_frame(buf: BoundedBuffer) -> bytes:
    return emit_frame_from_lines(buf.visible())






def suppress_post_clear_blanks(events: List[Tuple[float, bytes]]) -> List[Tuple[float, bytes]]:
    """
    Remove the blank-fill phase that caca/amp recordings emit after each clear.

    Pattern in the source:
        CSI 2J / H  +  long runs of spaces/newlines  +  CUP-drawn content

    The blank fills are what scriptreplay shows as "white flicker".  Dropping
    them (and accumulating their delay onto the next real content) makes the
    animation appear continuously without changing the visible art.
    """
    CLEAR_RE = re.compile(rb"\x1b\[(?:2J|3J|H)")
    # After a clear, stay in "blank mode" while we only see whitespace and CRs
    out: List[Tuple[float, bytes]] = []
    pending = 0.0
    blank_mode = False

    for delay, data in events:
        pending += delay
        if not data:
            continue

        # Split on clears so we can enter blank mode after each one
        pieces = []
        pos = 0
        for m in CLEAR_RE.finditer(data):
            if m.start() > pos:
                pieces.append((False, data[pos:m.start()]))
            pieces.append((True, m.group(0)))
            pos = m.end()
        if pos < len(data):
            pieces.append((False, data[pos:]))

        buf = bytearray()
        for is_clear, piece in pieces:
            if is_clear:
                buf.extend(piece)
                blank_mode = True
                continue
            if blank_mode:
                # Keep the piece only if it has non-whitespace content
                # or a CUP (absolute positioning for the art)
                if re.search(rb"\x1b\[\d*;\d*H", piece) or re.search(rb"[^ \t\r\n\x00]", piece):
                    # Content or CUP arrived – exit blank mode and keep everything from here
                    blank_mode = False
                    buf.extend(piece)
                # else pure whitespace – drop it (delay already accumulated)
            else:
                buf.extend(piece)

        if buf:
            out.append((pending, bytes(buf)))
            pending = 0.0

    if pending > 0 and out:
        d, data = out[-1]
        out[-1] = (d + pending, data)
    elif pending > 0:
        out.append((pending, b""))

    return out


def process(
    events: List[Tuple[float, bytes]],
    width: int,
    height: int,
    shift: int,
    stats: Optional[Stats],
) -> List[Tuple[float, bytes]]:
    """
    Stream-preserving trim for scriptreplay compatibility.

    Tracks cursor state through the original event stream.  Emits the same
    event boundaries and delays.  Printable characters that would land
    outside the W×H box (after optional horizontal shift) are dropped;
    their delay stays on the event so timing is preserved.  SGR, CUP,
    clears, and in-box printables are kept so scriptreplay output stays
    faithful to the source.

    When width/height are large enough that nothing is clipped, the output
    is a light-cleaned copy of the input (same event count, nearly the
    same bytes) and scriptreplay of the output matches the input.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    shift = max(0, int(shift))

    row = 0
    col = 0
    # carry incomplete ESC across events
    carry = ""
    out_events: List[Tuple[float, bytes]] = []
    # Header/footer lines are passed through unshifted so scriptreplay
    # still recognises the typescript.  Column shift applies only to the
    # session content that follows.  `in_header` stays true until the
    # header line's newline is seen (the header spans many events).
    past_header = False
    in_header_line = False
    # True when the previous event ended inside an escape sequence, so the
    # continuation bytes in this event are not printable content to shift.
    in_esc = False

    # One output event per input event.  The delay is always the original
    # event delay (never aggregated).  Printable characters that fall in
    # the shifted-off columns are dropped; control sequences and the
    # header are kept.  Incomplete sequences are left in their original
    # event so no event is emptied by carry.
    for delay, raw in events:
        # Light clean only – do not strip private modes / OSC, because the
        # source recording and the reference e2 example both keep them,
        # and removing them collapses events.
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin1", errors="replace")

        out_chars: List[str] = []
        i = 0
        n = len(text)

        while i < n:
            ch = text[i]

            # Detect and pass through script(1) header/footer lines.
            if not past_header and not in_header_line and ch != "\033":
                rest = text[i:]
                nl = rest.find("\n")
                candidate = rest[:nl] if nl >= 0 else rest
                if (candidate.startswith("Script started")
                        or candidate.startswith("Script done")):
                    in_header_line = True

            if in_header_line:
                out_chars.append(ch)
                if ch == "\n":
                    in_header_line = False
                    past_header = True
                    col = 0
                    row = 0
                i += 1
                continue

            if ch == "\033" or in_esc:
                if ch == "\033":
                    in_esc = True
                j = i + 1 if ch == "\033" else i
                if ch == "\033" and j >= n:
                    out_chars.append(ch)
                    i += 1
                    continue
                # Continuation of a CSI/OSC that started in a previous event,
                # or a fresh ESC.  Keep the bytes; do not treat them as
                # printables to be column-shifted.
                if ch != "\033":
                    # We're mid-sequence.  Emit until the terminator.
                    if text[i] == "[":
                        # CSI body
                        k = i + 1
                        while k < n and not (text[k].isalpha() or text[k] == "~"):
                            k += 1
                        if k >= n:
                            out_chars.append(text[i:])
                            break
                        out_chars.append(text[i:k + 1])
                        in_esc = False
                        i = k + 1
                        continue
                    # OSC or other – emit until BEL or end
                    k = i
                    while k < n and text[k] not in "\007\033":
                        k += 1
                    if k < n and text[k] == "\007":
                        k += 1
                        in_esc = False
                    out_chars.append(text[i:k])
                    i = k
                    continue
                # Fresh ESC
                if text[j] == "[":
                    k = j + 1
                    while k < n and not (text[k].isalpha() or text[k] == "~"):
                        k += 1
                    if k >= n:
                        out_chars.append(text[i:])
                        break
                    final = text[k]
                    params = text[j + 1:k]
                    seq = text[i:k + 1]
                    if final in ("H", "f"):
                        parts = params.split(";") if params else []
                        try:
                            r = int(parts[0]) if parts and parts[0] else 1
                        except ValueError:
                            r = 1
                        try:
                            c = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                        except ValueError:
                            c = 1
                        row = max(0, r - 1)
                        col = max(0, c - 1)
                    elif final == "J" and params in ("", "2", "3"):
                        row, col = 0, 0
                    out_chars.append(seq)
                    in_esc = False
                    i = k + 1
                    continue
                if text[j] == "]":
                    end = j + 1
                    while end < n:
                        if text[end] == "\007":
                            end += 1
                            in_esc = False
                            break
                        if text[end] == "\033" and end + 1 < n and text[end + 1] == "\\":
                            end += 2
                            in_esc = False
                            break
                        end += 1
                    else:
                        out_chars.append(text[i:])
                        break
                    out_chars.append(text[i:end])
                    i = end
                    continue
                out_chars.append(text[i:j + 1])
                in_esc = False
                i = j + 1
                continue

            if ch == "\r":
                col = 0
                out_chars.append(ch)
                i += 1
                continue
            if ch == "\n":
                row += 1
                col = 0
                out_chars.append(ch)
                i += 1
                continue
            if ch == "\b":
                if col > 0:
                    col -= 1
                out_chars.append(ch)
                i += 1
                continue
            if ord(ch) < 32 and ch not in "\t":
                out_chars.append(ch)
                i += 1
                continue

            # printable – drop if in the shifted-off columns or outside box
            box_col = col - shift
            if 0 <= row < height and 0 <= box_col < width:
                out_chars.append(ch)
            else:
                if stats is not None:
                    stats.add("clipped_char")
            col += 1
            i += 1

        data = "".join(out_chars).encode("utf-8", errors="replace")
        out_events.append((delay, data))

    return out_events



def write_output(
    events: List[Tuple[float, bytes]],
    timing_path: str,
    ts_path: str,
):
    """Write a classic script(1) timing + typescript pair.

    Event structure is written as-is (no synthetic header).  Adding a
    header broke identity tests and is unnecessary when the source
    typescript already plays under scriptreplay.
    """
    with open(timing_path, "w", encoding="utf-8") as tf, \
         open(ts_path, "wb") as sf:
        for delay, data in events:
            tf.write(f"{delay:.6f} {len(data)}\n")
            sf.write(data)


def kinder_missing(path: str, what: str) -> None:
    """Give a friendly, actionable error when a required file is missing."""
    if os.path.isfile(path):
        return
    print(f"amp-trim: cannot find {what} file:", file=sys.stderr)
    print(f"          {path}", file=sys.stderr)
    if not os.path.exists(path):
        print("  → The path does not exist.", file=sys.stderr)
        parent = os.path.dirname(path) or "."
        if os.path.isdir(parent):
            print(f"  → Directory “{parent}” exists; check the file name.", file=sys.stderr)
        else:
            print(f"  → Parent directory “{parent}” is also missing.", file=sys.stderr)
    elif os.path.isdir(path):
        print("  → That path is a directory, not a file.", file=sys.stderr)
    else:
        print("  → The path exists but is not a readable regular file.", file=sys.stderr)
    print("  Please supply a valid script(1) timing / typescript pair.", file=sys.stderr)
    sys.exit(2)


def main():
    p = argparse.ArgumentParser(
        prog="amp-trim",
        description="Trim a script typescript to a fixed WxH region "
                    "(with optional left shift), removing clear-screen "
                    "and absolute cursor sequences.  When width and height "
                    "are omitted, only the minimal bounding box is reported "
                    "(unless --shift is also given, in which case a shift-only "
                    "output can be written with -T/-S).  "
                    "Clear-screen sequences delimit animation frames so "
                    "height is max-per-frame, not the sum of all frames."
    )
    p.add_argument("-t", "--timing", required=True, help="Input timing file")
    p.add_argument("-s", "--typescript", required=True, help="Input typescript file")
    p.add_argument("-W", "--width", type=int, default=None,
                   help="Target width (omit together with --height to only detect the bounding box)")
    p.add_argument("-H", "--height", type=int, default=None,
                   help="Target height (omit together with --width to only detect the bounding box)")
    p.add_argument("-R", "--shift", type=int, default=0,
                   help="Shift the visible window to the right by this many "
                        "columns (default 0). Timing for the skipped columns "
                        "is retained. Can be used alone (with -T/-S) without "
                        "-W/-H; remaining content is kept at natural size.")
    p.add_argument("-T", "--out-timing", default=None, help="Output timing file (with -S: write; with -W/-H trim to that box, else near-identity copy)")
    p.add_argument("-S", "--out-typescript", default=None, help="Output typescript file (with -T: write; with -W/-H trim to that box, else near-identity copy)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print a detailed report of removed / shifted / "
                        "truncated characters and sequences")
    p.add_argument("-d", "--debug", action="store_true",
                   help="Print operational debug information: input files, "
                        "detected/requested size, frame counts, and whether "
                        "output files were written")
    args = p.parse_args()

    kinder_missing(args.timing, "timing")
    kinder_missing(args.typescript, "typescript")

    if (args.width is None) ^ (args.height is None):
        print("amp-trim: --width and --height must both be given, or both omitted",
              file=sys.stderr)
        sys.exit(2)

    if (args.out_timing is None) ^ (args.out_typescript is None):
        print("amp-trim: --out-timing (-T) and --out-typescript (-S) must both be given, or both omitted",
              file=sys.stderr)
        sys.exit(2)

    want_write = args.out_timing is not None and args.out_typescript is not None
    size_given = args.width is not None and args.height is not None

    # Modes:
    #   detect-only  – no -W/-H and no -T/-S  → print "W H" only
    #   auto-trim    – no -W/-H but -T/-S given → detect bbox, then trim & write
    #   trim         – -W/-H (and -T/-S required) → trim & write
    if size_given:
        if args.width < 1 or args.height < 1:
            print("amp-trim: width and height must be ≥ 1", file=sys.stderr)
            sys.exit(2)
        if not want_write:
            print("amp-trim: when trimming, both --out-timing (-T) and "
                  "--out-typescript (-S) are required",
                  file=sys.stderr)
            sys.exit(2)

    if args.shift < 0:
        print("amp-trim: shift must be ≥ 0", file=sys.stderr)
        sys.exit(2)

    events = parse_timing_typescript(args.timing, args.typescript)
    if not events:
        print("amp-trim: no events could be parsed from the input files.", file=sys.stderr)
        print("  Check that the timing file contains lines of the form:", file=sys.stderr)
        print("      <delay> <byte-count>", file=sys.stderr)
        print("  and that the typescript contains at least that many bytes.", file=sys.stderr)
        sys.exit(1)

    stats = Stats() if (args.verbose or args.debug) else None

    def debug_header():
        if not args.debug:
            return
        print("amp-trim: debug", file=sys.stderr)
        print(f"  input timing     : {args.timing}", file=sys.stderr)
        print(f"  input typescript : {args.typescript}", file=sys.stderr)
        print(f"  input events     : {len(events)}", file=sys.stderr)

    # Detect bounding box when size not given (detect-only or auto-trim)
    detected_w = detected_h = None
    if not size_given:
        detected_w, detected_h = detect_bbox(events, stats)
        print(f"W: {detected_w}  H: {detected_h}")
        print(f"amp-trim: minimal bounding box after cleaning: "
              f"{detected_w}×{detected_h}", file=sys.stderr)
        if not want_write:
            debug_header()
            if args.debug:
                print(f"  mode             : detect", file=sys.stderr)
                print(f"  detected width   : {detected_w}", file=sys.stderr)
                print(f"  detected height  : {detected_h}", file=sys.stderr)
                if args.shift:
                    print(f"  shift            : {args.shift} "
                          f"(would yield width ≈ {max(1, detected_w - args.shift)})",
                          file=sys.stderr)
                if stats:
                    frames = stats.counts.get("frame_boundary_clear/home", 0)
                    print(f"  clear boundaries : {frames}", file=sys.stderr)
                    print(f"  approx. frames   : {frames + 1 if frames else 1}",
                          file=sys.stderr)
                print(f"  output timing    : (not written – pass -T/-S to write)",
                      file=sys.stderr)
                print(f"  output typescript: (not written – pass -T/-S to write)",
                      file=sys.stderr)
            if args.verbose and stats:
                stats.report()
            return

        # No -W/-H but -T/-S given.
        if args.shift > 0:
            # Shift-only mode: discard the first N columns and keep the rest
            # at natural size (use a large width so nothing further is clipped).
            eff_w = max(1, (detected_w or 1) - args.shift)
            # Prefer a generous width so we don't accidentally clip the tail
            # if detection under-counted; 10000 is effectively unlimited for
            # terminal recordings.
            shift_width = max(eff_w, 10000)
            shift_height = max(detected_h or 1, 10000)
            trimmed = process(events, shift_width, shift_height, args.shift, stats)
            # Do not run suppress_post_clear_blanks here: it merges events
            # and aggregates delays.  Shift-only must keep the original
            # event count and the original per-event delays.
            try:
                write_output(trimmed, args.out_timing, args.out_typescript)
            except OSError as e:
                print(f"amp-trim: failed to write output files: {e}", file=sys.stderr)
                sys.exit(1)
            print(
                f"amp-trim: {len(events)} events → {len(trimmed)} events, "
                f"shift-only by {args.shift} columns "
                f"(natural size kept, detected was {detected_w}×{detected_h})",
                file=sys.stderr,
            )
            if args.debug:
                print("amp-trim: debug", file=sys.stderr)
                print(f"  mode             : shift-only", file=sys.stderr)
                print(f"  shift            : {args.shift}", file=sys.stderr)
                print(f"  detected width   : {detected_w}", file=sys.stderr)
                print(f"  detected height  : {detected_h}", file=sys.stderr)
                print(f"  output timing    : {args.out_timing}", file=sys.stderr)
                print(f"  output typescript: {args.out_typescript}", file=sys.stderr)
                print(f"  wrote timing     : yes", file=sys.stderr)
                print(f"  wrote typescript : yes", file=sys.stderr)
            if args.verbose and stats:
                stats.report()
            return

        # Pure identity copy of the parsed events so scriptreplay of the
        # output matches the input.  Detected size is reported above for
        # convenience and is NOT applied as a clip.
        # Apply blank-fill cleanup so caca/amp animations don't flicker white
        cleaned = suppress_post_clear_blanks(events)
        write_output(cleaned, args.out_timing, args.out_typescript)
        n = len(events)
        total = sum(len(d) for _, d in cleaned)
        print(f"amp-trim: {n} events → {len(cleaned)} events (identity + blank-fill cleanup, {total} bytes)",
              file=sys.stderr)
        if args.debug:
            print("amp-trim: debug", file=sys.stderr)
            print(f"  input timing     : {args.timing}", file=sys.stderr)
            print(f"  input typescript : {args.typescript}", file=sys.stderr)
            print(f"  mode             : identity", file=sys.stderr)
            print(f"  output timing    : {args.out_timing}", file=sys.stderr)
            print(f"  output typescript: {args.out_typescript}", file=sys.stderr)
            print(f"  wrote timing     : yes", file=sys.stderr)
            print(f"  wrote typescript : yes", file=sys.stderr)
        return

    trimmed = process(events, args.width, args.height, args.shift, stats)
    trimmed = suppress_post_clear_blanks(trimmed)
    try:
        write_output(trimmed, args.out_timing, args.out_typescript)
    except OSError as e:
        print(f"amp-trim: failed to write output files: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"amp-trim: {len(events)} events → {len(trimmed)} events, "
        f"bounded to {args.width}×{args.height}"
        + (f" (shift {args.shift})" if args.shift else ""),
        file=sys.stderr,
    )
    debug_header()
    if args.debug:
        print(f"  mode             : trim", file=sys.stderr)
        print(f"  requested width  : {args.width}", file=sys.stderr)
        print(f"  requested height : {args.height}", file=sys.stderr)
        if args.shift:
            print(f"  shift            : {args.shift}", file=sys.stderr)
        print(f"  output frames    : {len(trimmed)}", file=sys.stderr)
        if len(trimmed) > 1:
            sizes = {len(data) for _, data in trimmed if data}
            if len(sizes) == 1:
                print(f"  caca-style frames: {len(trimmed)} "
                      f"(uniform frame size {next(iter(sizes))} bytes)",
                      file=sys.stderr)
            else:
                print(f"  output events    : {len(trimmed)} (varying size)",
                      file=sys.stderr)
        print(f"  output timing    : {args.out_timing}", file=sys.stderr)
        print(f"  output typescript: {args.out_typescript}", file=sys.stderr)
        print(f"  wrote timing     : yes", file=sys.stderr)
        print(f"  wrote typescript : yes", file=sys.stderr)
    if args.verbose and stats:
        stats.report()


if __name__ == "__main__":
    main()
