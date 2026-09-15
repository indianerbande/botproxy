"""Das Fenster: Kopf, Anfragen, Antworten, Status.

`render` is a pure function — a snapshot and a size in, lines out — so the
layout is tested without a terminal. `Screen` is the thin shell around it:
it switches the console into full-screen mode, redraws a few times a second,
and puts the console back the way it found it.

No curses and no third-party library. Windows has no curses without an extra
package; ANSI escape sequences are understood by the Windows console since
Windows 10, once they are switched on — which `ctypes` can do.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from botproxy.monitor import Monitor, Snapshot, menge

MIN_WIDTH = 60
MIN_HEIGHT = 14
REDRAW_SECONDS = 0.25

# Alternate buffer, cursor hidden, line wrap off — so the last column can be
# written without the console moving the cursor to a line that is not there.
_ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[H\x1b[2J"
_LEAVE = "\x1b[?7h\x1b[?25h\x1b[?1049l"
_HOME = "\x1b[H"
_CLEAR = "\x1b[2J"


@dataclass(frozen=True)
class Header:
    """The three lines under the title. Worded by the caller, drawn here."""

    route: str
    state: str
    client: str


def render(snap: Snapshot, header: Header, width: int, height: int) -> list[str]:
    """Exactly `height` lines, none wider than `width`."""
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        hinweis = f"Fenster zu klein — mindestens {MIN_WIDTH} × {MIN_HEIGHT}"
        return [_fit(hinweis, width)] + [""] * (height - 1)

    inner = width - 2
    left = (inner - 1) // 2
    right = inner - 1 - left
    rows = height - 7
    log_rows = max(4, rows // 3)
    top_rows = rows - log_rows

    zahlen = (
        f"Anfragen {snap.total} · laufend {snap.running} · "
        f"↑ {menge(snap.sent)} · ↓ {menge(snap.received)}"
    )
    lines = [
        _rule("┌", "botproxy", "┐", inner),
        _row(f"{header.route} · {zahlen}", inner),
        _row(header.state, inner),
        _row(header.client, inner),
        "├" + _title("Anfragen", left) + "┬" + _title("Antworten", right) + "┤",
    ]
    anfragen = _last(snap.requests, top_rows)
    antworten = _last(snap.answers, top_rows)
    for links, rechts in zip(anfragen, antworten, strict=True):
        lines.append("│" + _cell(links, left) + "│" + _cell(rechts, right) + "│")
    lines.append("├" + _title("Status", left) + "┴" + "─" * right + "┤")
    wrapped = [part for entry in snap.log for part in _wrap(entry, inner - 2)]
    for zeile in _last(wrapped, log_rows):
        lines.append(_row(zeile, inner))
    lines.append("└" + "─" * inner + "┘")
    return lines


def _last(items: list[str], count: int) -> list[str]:
    """The newest `count` items, padded at the top so the newest sit at the bottom."""
    tail = items[-count:] if count else []
    return [""] * (count - len(tail)) + tail


def _fit(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(0, width - 1)] + "…"


def _cell(text: str, width: int) -> str:
    return " " + _fit(text, width - 1)


def _row(text: str, inner: int) -> str:
    return "│" + _cell(text, inner) + "│"


def _title(name: str, width: int) -> str:
    return _fit(f"─ {name} ".ljust(width, "─"), width)


def _rule(left: str, name: str, right: str, inner: int) -> str:
    return left + _title(name, inner) + right


def _wrap(text: str, width: int) -> list[str]:
    """Log lines wrap instead of being cut: a sign-in link must stay whole."""
    if width <= 0:
        return [text]
    teile = [text[i : i + width] for i in range(0, len(text), width)]
    return teile or [""]


def enable_ansi(stream: TextIO) -> str | None:
    """Switch escape sequences on. Returns why it cannot, or None when it can."""
    if not stream.isatty():
        return "die Ausgabe ist kein Konsolenfenster"
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return "die Konsole lässt sich nicht abfragen"
    virtual_terminal_processing = 0x0004
    if mode.value & virtual_terminal_processing:
        return None
    if not kernel32.SetConsoleMode(handle, mode.value | virtual_terminal_processing):
        return "diese Konsole versteht keine Steuerzeichen (vor Windows 10?)"
    return None


class Screen:
    """Full-screen redraw on a thread, until `stop`."""

    def __init__(
        self,
        monitor: Monitor,
        header: Callable[[], Header],
        stream: TextIO = sys.stdout,
    ) -> None:
        self._monitor = monitor
        self._header = header
        self._stream = stream
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._size: tuple[int, int] | None = None

    def start(self) -> str | None:
        """Take over the window. Returns why not, or None when it did."""
        grund = enable_ansi(self._stream)
        if grund is not None:
            return grund
        self._stream.write(_ENTER)
        self._stream.flush()
        self._thread = threading.Thread(
            target=self._loop, name="botproxy-screen", daemon=True
        )
        self._thread.start()
        return None

    def stop(self) -> None:
        """Hand the window back: normal buffer, cursor visible."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2)
        self._thread = None
        self._stream.write(_LEAVE)
        self._stream.flush()

    def _loop(self) -> None:
        while True:
            self.draw()
            if self._stop.wait(REDRAW_SECONDS):
                return

    def draw(self) -> None:
        size = shutil.get_terminal_size()
        lines = render(
            self._monitor.snapshot(), self._header(), size.columns, size.lines
        )
        # Overwrite in place instead of clearing: no flicker. Cleared only when
        # the window changed size and old lines could stick out.
        prefix = _HOME
        if self._size != (size.columns, size.lines):
            self._size = (size.columns, size.lines)
            prefix = _CLEAR + _HOME
        self._stream.write(prefix + "\r\n".join(lines))
        self._stream.flush()
