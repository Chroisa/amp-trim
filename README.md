# amp-trim

Clean, measure, and horizontally shift a `script(1)` timing + typescript pair so it can be embedded in an animated man page (see [amp](../amp)) or replayed with `scriptreplay`.

`amp-trim` keeps the original event structure and delays. Characters that fall outside the requested box (or in the shifted-off columns) are dropped; their timing stays with the event that produced them.

## Install

```bash
# from the amp-trim directory
sudo cp amp-trim /usr/local/bin/
sudo cp amp-trim.1 /usr/local/share/man/man1/
sudo mandb
```

Or run it in place:

```bash
./amp-trim.py -t in.t -s in.y
```

## Quick start

```bash
# Measure only – print the minimal bounding box
amp-trim -t demo.t -s demo.y

# Shift left by 5 columns (no width/height required)
amp-trim -t demo.t -s demo.y -R 5 -T out.t -S out.y

# Trim to an explicit box
amp-trim -t demo.t -s demo.y -W 48 -H 10 -T out.t -S out.y

# Trim and shift
amp-trim -t demo.t -s demo.y -W 40 -H 8 -R 2 -T out.t -S out.y
```

Replay the result:

```bash
scriptreplay -t out.t out.y
```

## Modes

| Mode | When | What it does |
|------|------|----------------|
| **Detect** | `-W`/`-H` omitted and no `-T`/`-S` | Prints the minimal width × height after cleaning. Clear-screen is a frame boundary, so height is the tallest frame, not the sum. |
| **Shift-only** | `-R N` with `-T`/`-S`, no `-W`/`-H` | Discards the first N columns. Keeps natural size. One output event per input event; delays are unchanged. |
| **Trim** | `-W` and `-H` given | Constrains content to that rectangle (optional `-R`). |
| **Identity** | `-T`/`-S` with no `-W`/`-H`/`-R` | Light clean + blank-fill removal. Event count and delays preserved. |

## Options

| Flag | Meaning |
|------|---------|
| `-t`, `--timing FILE` | Input timing file (required) |
| `-s`, `--typescript FILE` | Input typescript (required) |
| `-T`, `--out-timing FILE` | Output timing file |
| `-S`, `--out-typescript FILE` | Output typescript |
| `-W`, `--width N` | Visible width in columns |
| `-H`, `--height N` | Visible height in rows |
| `-R`, `--shift N` | Discard the first N columns. Works alone with `-T`/`-S`. |
| `--verbose` | Removal statistics |
| `--debug` | Extra diagnostics |
| `-h`, `--help` | Short help |

## Shift behaviour

`-R` / `--shift` drops columns `0 .. N-1` of the session content.

- The `Script started` / `Script done` header and footer are passed through unchanged so `scriptreplay` still honours the timing file.
- Control sequences (SGR, CUP, OSC, private modes) are kept, including sequences that span events.
- Printable characters in the shifted-off columns are removed.
- **Event count and per-event delays are identical to the input.** Timing is not aggregated.

```bash
./amp-trim.py -t e.t -s e.y -R 1 -T r.t -S r.y
# 20 events in → 20 events out, same delays, content shifted one column
```

## What is cleaned

Removed because they fight a surrounding pager:

- Erase-display (`CSI 2J` / `3J`)
- RIS (`ESC c` / `ESC C`)
- Relative cursor moves that would leave the box
- Form feeds

Kept:

- SGR / colour (`CSI … m`)
- CUP when it places content inside the box
- The script(1) header and footer

Blank-fill runs that caca-style recordings emit after a clear are dropped in identity and trim modes (their delay is preserved) so `scriptreplay` does not flash white between frames. Shift-only mode does not run this cleanup, so the original event boundaries stay intact.

## Typical amp workflow

```bash
# 1. Record
script -q -T demo.t demo.y
# … run the animation or command …
exit

# 2. Measure
amp-trim -t demo.t -s demo.y
# → W: 40  H: 8

# 3. Trim (and optionally shift off a prompt)
amp-trim -t demo.t -s demo.y -W 40 -H 8 -R 2 -T box.t -S box.y

# 4. Embed in a man page
# <<<ANIM_BOX w=40 h=8 id=A timing=box.t typescript=box.y>>>
# <<<END_ANIM_BOX>>>
```

## Files

| File | Purpose |
|------|---------|
| `amp-trim.py` | Implementation |
| `amp-trim` | Wrapper |
| `amp-trim.1` | Man page |
| `demo.t` / `demo.y`, `demo2.*`, `bt.*` | Sample recordings |

## See also

- [amp](../amp) – the pager shim that plays these typescripts inside a man page
- `script(1)`, `scriptreplay(1)`
