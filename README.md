# Walkman Remote

Small GTK3 desktop app to control music playing on an ADB-connected Android
device (built for the Sony NW-WM1AM2, but works with any device whose player
exposes a standard Android MediaSession).

![controls](https://img.shields.io/badge/prev%20%C2%B7%20play%2Fpause%20%C2%B7%20next-blue)

## Features

- Shows current track title, artist, album, and album art
- Walkman-style theme: black background, gold lettering
- Minimalist frameless window: no titlebar — **drag the middle to move, drag
  near an edge/corner to resize (cursor changes), press Escape to quit**
- Cover art scales with the window (shrinkable down to 120px)
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
| Metadata + playback state (polled every 2 s) | `adb shell dumpsys media_session` |
| Buttons | `adb shell cmd media_session dispatch <previous\|play-pause\|next>` |
| Album art | `adb exec-out content read --uri content://media/external/audio/albumart/<album_id>` |

The album art lookup maps the current title to an `album_id` via a cached
MediaStore query (`content query --uri content://media/external/audio/media
--projection title:album_id`), then reads the art JPEG directly from the
albumart content provider.

`cmd media_session dispatch` is used instead of `input keyevent 85/87/88`
because it reaches the media button session even after the player app has
gone idle and dropped its active session.
