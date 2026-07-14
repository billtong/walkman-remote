# Walkman Remote

Minimalist GTK3 desktop widget that shows what's playing on an ADB-connected
Android device (built for the Sony NW-WM1AM2, but works with any device whose
player exposes a standard Android MediaSession).

By default it is a display-only "now playing" panel: cover art with the track
title, artist, and album beneath it, styled after the Walkman's black-and-gold
look. Playback control code (prev / play-pause / next) is included but
commented out.

## Features

- Shows current track title, artist, album, and album art
- Walkman-style theme: black background, gold lettering
- Minimalist frameless window: no titlebar — **drag the middle to move, drag
  near an edge/corner to resize (cursor changes), press Escape to quit**
- Always on top of other windows
- Cover art scales with the window, pinned flush to the top edge
  (default 240px wide, shrinkable down to 120px)
- Survives USB unplug/replug (auto-retries in the background)
- Prev / Play-Pause / Next buttons and the playback-state status line exist
  but are commented out in `walkman_remote.py` (search for "hidden") —
  uncomment the blocks to restore them

## Requirements

- `adb` on PATH, device connected with USB debugging authorized
- System Python 3 with PyGObject/GTK3 (`python3-gi`, `gir1.2-gtk-3.0` —
  already present on Ubuntu). Note: run with `/usr/bin/python3`, not a venv.

## Run

```bash
/usr/bin/python3 ~/walkman-remote/walkman_remote.py
```

## How it works

| What | ADB command |
|---|---|
| Metadata + playback state (polled every 2 s, see `POLL_INTERVAL`) | `adb shell dumpsys media_session` |
| Playback controls (commented out by default) | `adb shell cmd media_session dispatch <previous\|play-pause\|next>` |
| Album art | `adb exec-out content read --uri content://media/external/audio/albumart/<album_id>`, falling back to `adb pull` + extracting the embedded ID3 `APIC` image for formats Android can't thumbnail (e.g. AIFF) |

All of these are read-only or equivalent to pressing the device's own media
buttons — nothing is written to or installed on the device.

The album art lookup maps the current title to an `album_id` via a cached
MediaStore query (`content query --uri content://media/external/audio/media
--projection title:album_id`), then reads the art JPEG directly from the
albumart content provider.

`cmd media_session dispatch` is used instead of `input keyevent 85/87/88`
because it reaches the media button session even after the player app has
gone idle and dropped its active session.

## Implementation notes

- The script forces `GDK_BACKEND=x11` before GTK loads: always-on-top
  (`set_keep_above`) is ignored for Wayland-native windows, so on a Wayland
  session the app runs under XWayland where the window manager honors it.
- The art is painted onto a `Gtk.DrawingArea` kept square by matching its
  height request to its allocated width — a fixed `Gtk.Image` would impose a
  hard minimum window size and block shrinking.
