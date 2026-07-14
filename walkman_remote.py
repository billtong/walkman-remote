#!/usr/bin/python3
"""Walkman Remote — control music on an ADB-connected Android device (Sony NW-WM1AM2).

Shows the currently playing track (title / artist / album + album art) and
provides prev / play-pause / next buttons. All communication goes over adb:

  - metadata + playback state:  adb shell dumpsys media_session
  - controls:                    adb shell cmd media_session dispatch <cmd>
  - album art:                  adb exec-out content read --uri \
                                    content://media/external/audio/albumart/<album_id>

Run with the system python (needs PyGObject/GTK3):
  /usr/bin/python3 walkman_remote.py
"""

import os
import re
import subprocess
import tempfile
import threading

# keep-above is not possible for Wayland-native windows, so force the X11
# backend (runs under XWayland on a Wayland session). This must happen
# before GTK is imported — PyGObject initializes GTK (and picks the
# backend) at import time, which is too late for Gdk.set_allowed_backends.
os.environ["GDK_BACKEND"] = "x11"

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

POLL_INTERVAL = 2.0  # seconds between dumpsys polls
ADB_TIMEOUT = 10     # seconds before an adb call is considered hung

# Commands for `adb shell cmd media_session dispatch <cmd>`. These reach the
# media button session even when it has gone inactive (paused/idle), where raw
# `input keyevent` media keys are sometimes dropped.
CMD_PLAY_PAUSE = "play-pause"
CMD_NEXT = "next"
CMD_PREV = "previous"

# Walkman-style theme: black background, gold lettering (like the
# NW-WM1AM2 hardware and its player UI).
WALKMAN_CSS = b"""
window {
    background-color: #000000;
}
label {
    color: #c9a24b;
}
label.dim-label {
    color: #8a6f33;
}
"""

STATE_NAMES = {
    0: "no media",
    1: "stopped",
    2: "paused",
    3: "playing",
    6: "buffering",
    7: "error",
}


def adb(*args, binary=False, timeout=ADB_TIMEOUT):
    """Run an adb command; return stdout (str or bytes) or None on failure."""
    try:
        result = subprocess.run(
            ["adb", *args],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8", errors="replace")


def parse_media_session(dump):
    """Extract (title, artist, album, state) from `dumpsys media_session` output.

    Only sessions inside the "Sessions Stack" are considered; ones marked
    active=true win, otherwise any session that has metadata is used (the
    player session can go inactive while paused). The metadata line looks like:
        metadata: size=8, description=<title>, <artist>, <album>
    The title may itself contain ", ", so the artist/album are taken as the
    two trailing comma-separated segments.
    """
    stack = dump.split("Sessions Stack", 1)
    if len(stack) < 2:
        return None
    fallback = None
    for session in re.split(r"\n(?=    \S)", stack[1]):
        meta = re.search(r"metadata: size=\d+, description=(.*)", session)
        if not meta:
            continue
        state = re.search(r"state=PlaybackState \{state=(\d+)", session)
        parts = meta.group(1).rsplit(", ", 2)
        while len(parts) < 3:
            parts.append("")
        title, artist, album = (
            "" if p.strip() == "null" else p.strip() for p in parts)
        info = (title, artist, album, int(state.group(1)) if state else 0)
        if "active=true" in session:
            return info
        fallback = fallback or info
    return fallback


def extract_embedded_art(data):
    """Extract APIC image bytes from an ID3v2 tag embedded in a media file.

    Handles AIFF (art lives in an 'ID3 ' chunk that Android's thumbnailer
    ignores, so the albumart provider fails for these) and anything with a
    plain ID3v2 tag (e.g. MP3). Returns JPEG/PNG bytes or None.
    """
    id3 = None
    if data[:4] == b"FORM":
        off = 12
        while off + 8 <= len(data):
            cid = data[off:off + 4]
            size = int.from_bytes(data[off + 4:off + 8], "big")
            if cid in (b"ID3 ", b"id3 "):
                id3 = data[off + 8:off + 8 + size]
                break
            off += 8 + size + (size & 1)
    else:
        idx = data.find(b"ID3")
        if idx >= 0:
            id3 = data[idx:]
    if not id3 or id3[:3] != b"ID3":
        return None
    major = id3[3]
    pos = 10
    while pos + 10 <= len(id3):
        frame_id = id3[pos:pos + 4]
        raw = id3[pos + 4:pos + 8]
        if major >= 4:  # v2.4 uses syncsafe frame sizes
            size = 0
            for byte in raw:
                size = (size << 7) | (byte & 0x7F)
        else:
            size = int.from_bytes(raw, "big")
        if size <= 0:
            return None
        if frame_id == b"APIC":
            frame = id3[pos + 10:pos + 10 + size]
            for magic in (b"\xff\xd8\xff", b"\x89PNG"):
                start = frame.find(magic)
                if start >= 0:
                    return frame[start:]
        pos += 10 + size
    return None


def norm(text):
    """Normalize a title/album for fuzzy matching: case-fold, strip a
    leading track-number prefix ("02. ", "3 - ", …), collapse whitespace."""
    text = re.sub(r"^\s*\d{1,3}\s*[.\-_]?\s+", "", text.strip().lower())
    return re.sub(r"\s+", " ", text)


class ArtResolver:
    """Maps track titles to album art bytes via the device MediaStore."""

    def __init__(self):
        self._title_to_album = {}   # exact title -> (album_id, file path)
        self._norm_index = {}       # norm(title) -> [(norm(album), id, path)]
        self._art_cache = {}        # album_id -> bytes or None

    def _refresh_library(self):
        out = adb(
            "shell", "content", "query",
            "--uri", "content://media/external/audio/media",
            "--projection", "title:album:album_id:_data",
            timeout=30,
        )
        if not out:
            return
        # Row format:
        #   "Row: N title=<title>, album=<album>, album_id=<id>, _data=<path>"
        # title/album can contain commas, so anchor on the fixed-format
        # album_id instead of splitting on commas.
        self._title_to_album = {}
        self._norm_index = {}
        for m in re.finditer(
                r"title=(.*), album=(.*), album_id=(\d+), _data=(.*)$",
                out, re.M):
            title, album, album_id, path = m.groups()
            self._title_to_album[title] = (album_id, path.strip())
            self._norm_index.setdefault(norm(title), []).append(
                (norm(album), album_id, path.strip()))

    def _lookup(self, title, album):
        """Find the album_id for a track, tolerating tag differences.

        The player's session metadata and MediaStore often disagree on the
        exact title (e.g. "WINDY SUMMER" vs "02. WINDY SUMMER") or album
        ("TIMELY!! [Remaster]" vs "Timely!!"), so fall back from exact title
        match to normalized title match, using the album name to pick among
        candidates when it helps.
        """
        if title in self._title_to_album:
            return self._title_to_album[title]
        candidates = self._norm_index.get(norm(title), [])
        if not candidates:
            return None
        want = norm(album)
        if want:
            for got, album_id, path in candidates:
                if got and (want in got or got in want):
                    return (album_id, path)
        return candidates[0][1:]

    def _embedded_art(self, path):
        """Pull the audio file off the device and extract its embedded art.

        Fallback for formats whose art Android's albumart provider cannot
        thumbnail (e.g. AIFF). `adb pull` is used rather than `exec-out cat`
        because pull needs no device-shell quoting of exotic file names.
        """
        if not path:
            return None
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "track")
            if adb("pull", path, local, timeout=120) is None:
                return None
            try:
                with open(local, "rb") as fh:
                    return extract_embedded_art(fh.read())
            except OSError:
                return None

    def art_for(self, title, album=""):
        """Return JPEG/PNG bytes for the track's album, or None."""
        if self._lookup(title, album) is None:
            self._refresh_library()
        found = self._lookup(title, album)
        if found is None:
            return None
        album_id, path = found
        if album_id not in self._art_cache:
            data = adb(
                "exec-out", "content", "read",
                "--uri", f"content://media/external/audio/albumart/{album_id}",
                binary=True, timeout=30,
            )
            # A failed read returns a text error message, not image bytes.
            if not data or not data.startswith((b"\xff\xd8", b"\x89PNG")):
                data = self._embedded_art(path)
            self._art_cache[album_id] = data
        return self._art_cache[album_id]


class WalkmanRemote(Gtk.Window):
    ART_SIZE = 240        # initial art width; scales with the window
    ART_MIN = 120         # smallest the window can be shrunk to
    RESIZE_MARGIN = 16    # grab zone (px) along the edges for resizing

    # (column, row) zone of the pointer -> resize edge / cursor name.
    # Column/row are 0 (near left/top edge), 1 (middle), 2 (near right/bottom).
    EDGES = {
        (0, 0): Gdk.WindowEdge.NORTH_WEST, (1, 0): Gdk.WindowEdge.NORTH,
        (2, 0): Gdk.WindowEdge.NORTH_EAST, (0, 1): Gdk.WindowEdge.WEST,
        (2, 1): Gdk.WindowEdge.EAST, (0, 2): Gdk.WindowEdge.SOUTH_WEST,
        (1, 2): Gdk.WindowEdge.SOUTH, (2, 2): Gdk.WindowEdge.SOUTH_EAST,
    }
    CURSOR_NAMES = {
        (0, 0): "nw-resize", (1, 0): "n-resize", (2, 0): "ne-resize",
        (0, 1): "w-resize", (2, 1): "e-resize",
        (0, 2): "sw-resize", (1, 2): "s-resize", (2, 2): "se-resize",
    }

    def __init__(self):
        super().__init__(title="Walkman Remote")
        # No border padding so the cover art sits edge-to-edge. Enough
        # default height that the art starts roughly square.
        self.set_default_size(self.ART_SIZE, self.ART_SIZE + 80)
        self.connect("destroy", Gtk.main_quit)

        # Minimalist frameless window: no titlebar. Drag the middle to move,
        # drag near an edge/corner to resize, press Escape to quit.
        self.set_decorated(False)
        # Pin above all other windows (needs the X11 backend, see main()).
        self.set_keep_above(True)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("button-press-event", self._on_drag)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("key-press-event", self._on_key)

        provider = Gtk.CssProvider()
        provider.load_from_data(WALKMAN_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._art = ArtResolver()
        self._current = None          # (title, artist, album, state)
        self._current_art_title = None
        self._poke = threading.Event()  # set to force an immediate re-poll

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(box)

        # Art is painted onto a DrawingArea so it rescales with the window
        # (a fixed Gtk.Image would impose a hard minimum window size and
        # block shrinking). It is kept square by forcing its height request
        # to match its allocated width, and packed non-expanding at the top
        # so no gap appears above it — leftover space goes below the labels.
        self._art_pixbuf = None
        self.art_area = Gtk.DrawingArea()
        self.art_area.set_size_request(self.ART_MIN, self.ART_MIN)
        self.art_area.connect("draw", self._on_draw_art)
        self.art_area.connect("size-allocate", self._on_art_allocate)
        self.art_area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK)
        self.art_area.connect("button-press-event", self._on_drag)
        self.art_area.connect("motion-notify-event", self._on_motion)
        box.pack_start(self.art_area, False, False, 0)

        self.title_label = Gtk.Label(label="—")
        self.title_label.set_margin_top(12)
        self.title_label.set_line_wrap(True)
        self.title_label.set_justify(Gtk.Justification.CENTER)
        self.title_label.set_markup("<span size='x-large' weight='bold'>—</span>")
        box.pack_start(self.title_label, False, False, 0)

        self.artist_label = Gtk.Label(label="")
        self.album_label = Gtk.Label(label="")
        self.artist_label.set_line_wrap(True)
        self.album_label.set_line_wrap(True)
        box.pack_start(self.artist_label, False, False, 0)
        box.pack_start(self.album_label, False, False, 0)

        # Control buttons hidden to match the Walkman display-only UI.
        # Uncomment to restore prev / play-pause / next buttons.
        # controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        # controls.set_halign(Gtk.Align.CENTER)
        # for icon, cmd in (
        #     ("media-skip-backward-symbolic", CMD_PREV),
        #     ("media-playback-start-symbolic", CMD_PLAY_PAUSE),
        #     ("media-skip-forward-symbolic", CMD_NEXT),
        # ):
        #     button = Gtk.Button()
        #     button.set_image(
        #         Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG))
        #     button.connect("clicked", self._on_control, cmd)
        #     controls.pack_start(button, False, False, 0)
        # box.pack_start(controls, False, False, 8)

        # Status line ("playing" / "paused" / disconnected) hidden for the
        # minimalist UI. Uncomment here and in _show_disconnected/_show_info
        # to restore it.
        # self.status_label = Gtk.Label(label="connecting…")
        # self.status_label.get_style_context().add_class("dim-label")
        # box.pack_end(self.status_label, False, False, 0)

        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _on_art_allocate(self, widget, alloc):
        # Keep the art square: request height equal to the allocated width.
        # The guard prevents an allocate/request feedback loop.
        height = max(alloc.width, self.ART_MIN)
        if widget.get_size_request()[1] != height:
            widget.set_size_request(self.ART_MIN, height)

    def _on_draw_art(self, widget, cr):
        alloc = widget.get_allocation()
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        if self._art_pixbuf is not None:
            cr.scale(alloc.width / self._art_pixbuf.get_width(),
                     alloc.height / self._art_pixbuf.get_height())
            Gdk.cairo_set_source_pixbuf(cr, self._art_pixbuf, 0, 0)
            cr.paint()
        return False

    # ---- frameless window handling ----

    def _zone(self, x, y):
        """Which edge zone of the window the point is in (see EDGES)."""
        width, height = self.get_size()
        margin = self.RESIZE_MARGIN
        col = 0 if x < margin else (2 if x > width - margin else 1)
        row = 0 if y < margin else (2 if y > height - margin else 1)
        return (col, row)

    def _on_drag(self, _widget, event):
        if event.button != 1:
            return False
        win_x, win_y = self.get_position()
        edge = self.EDGES.get(
            self._zone(event.x_root - win_x, event.y_root - win_y))
        if edge is not None:
            self.begin_resize_drag(
                edge, event.button,
                int(event.x_root), int(event.y_root), event.time)
        else:
            self.begin_move_drag(
                event.button,
                int(event.x_root), int(event.y_root), event.time)
        return False

    def _on_motion(self, _widget, event):
        win_x, win_y = self.get_position()
        name = self.CURSOR_NAMES.get(
            self._zone(event.x_root - win_x, event.y_root - win_y))
        if event.window:
            event.window.set_cursor(
                Gdk.Cursor.new_from_name(self.get_display(), name)
                if name else None)
        return False

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            Gtk.main_quit()
        return False

    # ---- controls ----

    def _on_control(self, _button, cmd):
        def send():
            adb("shell", "cmd", "media_session", "dispatch", cmd)
            self._poke.set()
        threading.Thread(target=send, daemon=True).start()

    # ---- polling ----

    def _poll_loop(self):
        while True:
            dump = adb("shell", "dumpsys", "media_session")
            if dump is None:
                GLib.idle_add(self._show_disconnected)
            else:
                info = parse_media_session(dump)
                art = None
                if info and info[0] and info[0] != self._current_art_title:
                    art = self._art.art_for(info[0], info[2])
                GLib.idle_add(self._show_info, info, art)
            self._poke.wait(POLL_INTERVAL)
            self._poke.clear()

    # ---- UI updates (main thread) ----

    def _show_disconnected(self):
        # self.status_label.set_text("device disconnected — retrying…")
        pass

    def _show_info(self, info, art):
        if info is None:
            # self.status_label.set_text("connected — no active media session")
            return
        title, artist, album, state = info
        if info != self._current:
            self._current = info
            self.title_label.set_markup(
                "<span size='x-large' weight='bold'>{}</span>".format(
                    GLib.markup_escape_text(title or "—")))
            self.artist_label.set_text(artist)
            self.album_label.set_text(album)
        if title and title != self._current_art_title:
            self._current_art_title = title
            self._art_pixbuf = None
            if art:
                loader = GdkPixbuf.PixbufLoader()
                try:
                    loader.write(art)
                    loader.close()
                    self._art_pixbuf = loader.get_pixbuf()
                except GLib.Error:
                    pass
            self.art_area.queue_draw()
        # self.status_label.set_text(STATE_NAMES.get(state, f"state {state}"))


def main():
    window = WalkmanRemote()
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
