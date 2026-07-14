# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file GTK3 desktop widget (`walkman_remote.py`) that shows the
now-playing track of a USB-connected Android device — built for a Sony
Walkman NW-WM1AM2 — entirely over `adb`. No build system, no dependencies
beyond system PyGObject, no test suite.

## Running

```bash
/usr/bin/python3 walkman_remote.py
```

- **Must** be the system Python: PyGObject/GTK3 (`python3-gi`) is installed
  there and typically missing from any active venv. Plain `python3` may
  resolve to a venv.
- Requires the device connected with USB debugging authorized (`adb devices`).
- The window is frameless: drag middle to move, drag edges to resize,
  **Escape to quit**.
- To restart a running instance: `pgrep -f "walkman_[r]emote" | xargs -r kill`
  — the `[r]` bracket is load-bearing; a plain pattern also matches the shell
  command that contains the filename and kills your own shell. For the same
  reason, run the kill and the relaunch as **separate** shell invocations.

There are no lint/test commands. Verify changes by importing the module and
exercising the pure parts against the live device, e.g.:

```bash
/usr/bin/python3 -c "
import subprocess, walkman_remote as w
dump = subprocess.run(['adb','shell','dumpsys','media_session'],
                      capture_output=True).stdout.decode()
print(w.parse_media_session(dump))"
```

## Architecture

One module, three layers:

1. **ADB layer** — `adb()` wrapper (subprocess, returns None on any failure),
   `parse_media_session()` (pure function over `dumpsys media_session` text),
   `extract_embedded_art()` (pure function over media-file bytes).
2. **`ArtResolver`** — maps session title/album → MediaStore `album_id` →
   art bytes, with per-album caching.
3. **`WalkmanRemote(Gtk.Window)`** — UI. A daemon thread polls every
   `POLL_INTERVAL` seconds; all UI mutation is marshalled to the main loop
   via `GLib.idle_add`. `self._poke` (threading.Event) forces an immediate
   re-poll after a control is dispatched.

### Non-obvious constraints (violating these reintroduces fixed bugs)

- `os.environ["GDK_BACKEND"] = "x11"` must run **before** `import gi`
  makes GTK available: PyGObject picks the display backend at import time,
  and the always-on-top flag (`set_keep_above`) is silently ignored for
  Wayland-native windows. `Gdk.set_allowed_backends()` in `main()` is too
  late.
- Media controls use `adb shell cmd media_session dispatch <cmd>`, not
  `input keyevent`: the Sony player process periodically restarts and drops
  its active session, and raw media keyevents are dropped in that state
  while the dispatcher still reaches it. `parse_media_session` likewise must
  keep its fallback to inactive sessions.
- Cover art has a **two-stage lookup and a two-stage fetch**, all needed:
  - Session metadata titles/albums disagree with MediaStore tags
    ("WINDY SUMMER" vs "02. WINDY SUMMER", "TIMELY!! [Remaster]" vs
    "Timely!!"), so exact title match falls back to `norm()`-alized match
    disambiguated by album-name containment.
  - The albumart content provider cannot thumbnail some formats (AIFF:
    `FileNotFoundException: Failed to create thumbnail`), so the fallback
    pulls the audio file and extracts the ID3 `APIC` frame itself.
    Use `adb pull` for device files — `exec-out cat` breaks on paths with
    spaces/quotes because adb's arg joining defeats shell quoting.
- MediaStore query output is parsed with regexes anchored on the trailing
  fixed-format fields (`album_id=(\d+)…`), never comma-split: titles and
  albums contain ", ". Same idea in `parse_media_session` (rsplit twice).
- The art is a `Gtk.DrawingArea` whose height request tracks its allocated
  width (`_sync_art_height`), **not** a `Gtk.Image`: a Gtk.Image's pixbuf
  size acts as a hard minimum window size and blocks shrinking. The guard
  comparing against the current size request prevents an allocate/request
  feedback loop.
- The prev/play-pause/next buttons and the status line are intentionally
  commented out (minimalist display-only UI), not dead code — keep them
  compiling in spirit when refactoring the code they reference
  (`_on_control`, `CMD_*`, `STATE_NAMES`).

## Device-safety expectations

Everything sent to the device is read-only or equivalent to pressing its own
media buttons. Keep it that way: no writes to device storage, no settings
changes, no package installs.
