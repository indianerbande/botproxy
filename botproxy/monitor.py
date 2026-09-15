"""Was durch botproxy fließt — als Verkehrsdaten, nie als Inhalt.

Holds what the screen shows: one line per request, one live line per answer,
and the status log. It knows sizes, times and status codes. It never sees a
body or a header value, so it cannot show what it must not: no token, no
source code, and nothing parsed out of the payload.

Thread-safe: request threads report, the screen thread reads.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

KEEP = 200


@dataclass
class _Answer:
    """One request's way back, updated while it streams."""

    rid: int
    started: float
    status: int | None = None
    code: str | None = None
    first_chunk: float | None = None
    ended: float | None = None
    chunks: int = 0
    received: int = 0
    aborted: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """Everything the screen draws, already worded. Newest lines last."""

    requests: list[str]
    answers: list[str]
    log: list[str]
    total: int
    running: int
    sent: int
    received: int


@dataclass
class Monitor:
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = datetime.now
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _requests: deque[str] = field(default_factory=lambda: deque(maxlen=KEEP))
    _answers: deque[_Answer] = field(default_factory=lambda: deque(maxlen=KEEP))
    _log: deque[str] = field(default_factory=lambda: deque(maxlen=KEEP))
    _by_id: dict[int, _Answer] = field(default_factory=dict)
    _next: int = 1
    _sent: int = 0
    _received: int = 0

    # --- Melden ---------------------------------------------------------------

    def log(self, message: str) -> None:
        if not message.strip():
            return
        with self._lock:
            self._log.append(f"{self._stamp()} {message}")

    def request(self, method: str, path: str, size: int | None) -> int:
        """A request came in. Returns the number that pairs it with its answer.

        The query string is not shown, only that there was one: it is the one
        part of a URL where a client might carry something private.
        """
        shown, _, query = path.partition("?")
        if query:
            shown += "?…"
        groesse = f" · {menge(size)}" if size else ""
        with self._lock:
            rid = self._next
            self._next += 1
            self._sent += size or 0
            self._requests.append(f"{self._stamp()} #{rid} {method} {shown}{groesse}")
            answer = _Answer(rid=rid, started=self.clock())
            self._answers.append(answer)
            self._by_id[rid] = answer
        return rid

    def answering(self, rid: int, status: int) -> None:
        with self._lock:
            if answer := self._by_id.get(rid):
                answer.status = status

    def chunk(self, rid: int, size: int) -> None:
        with self._lock:
            if answer := self._by_id.get(rid):
                if answer.first_chunk is None:
                    answer.first_chunk = self.clock()
                answer.chunks += 1
                answer.received += size
                self._received += size

    def finished(self, rid: int) -> None:
        self._end(rid)

    def rejected(self, rid: int, status: int, code: str) -> None:
        with self._lock:
            if answer := self._by_id.get(rid):
                answer.status = status
                answer.code = code
        self._end(rid)

    def aborted(self, rid: int, reason: str) -> None:
        with self._lock:
            if answer := self._by_id.get(rid):
                answer.aborted = reason
        self._end(rid)

    def _end(self, rid: int) -> None:
        with self._lock:
            if answer := self._by_id.pop(rid, None):
                answer.ended = self.clock()

    # --- Lesen ----------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            now = self.clock()
            return Snapshot(
                requests=list(self._requests),
                answers=[_describe(a, now) for a in self._answers],
                log=list(self._log),
                total=self._next - 1,
                running=len(self._by_id),
                sent=self._sent,
                received=self._received,
            )

    def _stamp(self) -> str:
        return self.wall().strftime("%H:%M:%S")


def _describe(answer: _Answer, now: float) -> str:
    """One line for one answer, in the words of its current state."""
    head = f"#{answer.rid}" + (f" {answer.status}" if answer.status else "")
    end = answer.ended if answer.ended is not None else now
    dauer_bisher = dauer(end - answer.started)
    if answer.code:
        return f"{head} {answer.code}"
    if answer.aborted:
        return f"{head} {answer.aborted} nach {dauer_bisher} · {menge(answer.received)}"
    if answer.first_chunk is None:
        # Headers may be there, the first token is not: the model is still
        # reading the prompt. The one wait worth seeing.
        wort = "ohne Inhalt" if answer.ended is not None else "wartet"
        return f"{head} {wort} {dauer_bisher}"
    erstes = dauer(answer.first_chunk - answer.started)
    if answer.ended is not None:
        stuecke = "Stück" if answer.chunks == 1 else "Stücken"
        return (
            f"{head} fertig {dauer_bisher} · {menge(answer.received)} in "
            f"{answer.chunks} {stuecke} · erstes nach {erstes}"
        )
    laufzeit = now - answer.first_chunk
    rate = f" · {menge(int(answer.received / laufzeit))}/s" if laufzeit > 0.2 else ""
    return f"{head} läuft {dauer_bisher} · {menge(answer.received)}{rate}"


def menge(size: int | None) -> str:
    """Bytes in German: "812 B", "4,2 KB", "38 KB", "1,4 MB"."""
    if size is None:
        return "–"
    if size < 1024:
        return f"{size} B"
    for einheit, teiler in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        wert = size / teiler
        if wert < 1024 or einheit == "GB":
            text = f"{wert:.1f}" if wert < 10 else f"{wert:.0f}"
            return f"{text.replace('.', ',')} {einheit}"
    raise AssertionError("unreachable")


def dauer(seconds: float) -> str:
    """A span in German: "412 ms", "3,1 s", "2:05 min"."""
    if seconds < 1:
        return f"{max(0, round(seconds * 1000))} ms"
    if seconds < 60:
        return f"{seconds:.1f} s".replace(".", ",")
    minuten, rest = divmod(int(seconds), 60)
    return f"{minuten}:{rest:02d} min"
