"""Die Anzeige: was sie zeigt, wie sie es anordnet — und was nie darin steht."""

from __future__ import annotations

import io
import threading
from datetime import datetime

import pytest

from botproxy import monitor, screen, server
from botproxy.monitor import Monitor, dauer, menge
from tests.test_server import ruf


class Uhr:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def uhr():
    return Uhr()


@pytest.fixture
def mon(uhr):
    return Monitor(clock=uhr, wall=lambda: datetime(2026, 9, 15, 20, 52, 5))


HEADER = screen.Header(
    route="http://127.0.0.1:8127  →  http://localhost:1234/v1",
    state="Token gültig bis 21:14 · Endpunkt antwortet, 3 Modelle",
    client="Client: Base URL http://127.0.0.1:8127/v1 · API-Key xyz",
)


# --- Worte ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (812, "812 B"),
        (4300, "4,2 KB"),
        (38 * 1024, "38 KB"),
        (int(1.4 * 1024**2), "1,4 MB"),
    ],
)
def test_menge(size, text):
    assert menge(size) == text


@pytest.mark.parametrize(
    ("seconds", "text"), [(0.412, "412 ms"), (3.14, "3,1 s"), (125, "2:05 min")]
)
def test_dauer(seconds, text):
    assert dauer(seconds) == text


# --- Monitor ----------------------------------------------------------------


def test_eine_anfrage_von_anfang_bis_ende(mon, uhr):
    rid = mon.request("POST", "/v1/chat/completions", 182 * 1024)
    assert mon.snapshot().answers == [f"#{rid} wartet 0 ms"]

    uhr.t += 3.1
    mon.answering(rid, 200)
    assert mon.snapshot().answers == [f"#{rid} 200 wartet 3,1 s"]

    mon.chunk(rid, 1024)
    uhr.t += 1.0
    mon.chunk(rid, 1024)
    laufend = mon.snapshot()
    assert laufend.answers == [f"#{rid} 200 läuft 4,1 s · 2,0 KB · 2,0 KB/s"]
    assert laufend.running == 1

    uhr.t += 1.0
    mon.finished(rid)
    fertig = mon.snapshot()
    assert fertig.answers == [
        f"#{rid} 200 fertig 5,1 s · 2,0 KB in 2 Stücken · erstes nach 3,1 s"
    ]
    assert fertig.requests == [f"20:52:05 #{rid} POST /v1/chat/completions · 182 KB"]
    assert (fertig.total, fertig.running, fertig.sent, fertig.received) == (
        1,
        0,
        182 * 1024,
        2048,
    )


def test_abgewiesen_und_abgebrochen(mon, uhr):
    a = mon.request("POST", "/v1/chat/completions", 2)
    mon.rejected(a, 403, "forbidden_key")
    b = mon.request("POST", "/v1/chat/completions", 2)
    mon.answering(b, 200)
    mon.chunk(b, 100)
    uhr.t += 2
    mon.aborted(b, "Client weg")

    assert mon.snapshot().answers == [
        f"#{a} 403 forbidden_key",
        f"#{b} 200 Client weg nach 2,0 s · 100 B",
    ]
    assert mon.snapshot().running == 0


def test_query_wird_nicht_gezeigt(mon):
    mon.request("GET", "/v1/models?api_key=geheim", None)
    zeile = mon.snapshot().requests[-1]
    assert "geheim" not in zeile
    assert zeile.endswith("/v1/models?…")


def test_log_behaelt_nur_die_letzten(mon):
    for i in range(monitor.KEEP + 50):
        mon.log(f"Meldung {i}")
    log = mon.snapshot().log
    assert len(log) == monitor.KEEP
    assert log[-1].endswith(f"Meldung {monitor.KEEP + 49}")


# --- Zeichnen ---------------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), [(60, 14), (100, 30), (220, 60)])
def test_genau_so_gross_wie_das_fenster(mon, width, height):
    for i in range(40):
        rid = mon.request("POST", "/v1/chat/completions/sehr/langer/pfad" * 3, 999)
        mon.finished(rid)
        mon.log(f"Meldung {i} " + "lang " * 30)
    zeilen = screen.render(mon.snapshot(), HEADER, width, height)
    assert len(zeilen) == height
    assert all(len(z) == width for z in zeilen)


def test_bereiche_stehen_wo_sie_hingehoeren(mon):
    rid = mon.request("POST", "/v1/chat/completions", 1024)
    mon.answering(rid, 200)
    mon.log("Token erneuert")
    zeilen = screen.render(mon.snapshot(), HEADER, 100, 24)

    assert zeilen[0].startswith("┌─ botproxy ")
    assert "localhost:1234" in zeilen[1]
    assert "Anfragen 1 · laufend 1" in zeilen[1]
    assert "Token gültig" in zeilen[2]
    assert "API-Key" in zeilen[3]
    kopf = next(z for z in zeilen if "Anfragen" in z and "Antworten" in z)
    mitte = kopf.index("┬")
    zeile = next(z for z in zeilen if f"#{rid} POST" in z)
    assert zeile.index(f"#{rid} POST") < mitte < zeile.index(f"#{rid} 200")
    status = next(i for i, z in enumerate(zeilen) if "Status" in z)
    assert any("Token erneuert" in z for z in zeilen[status:])
    assert zeilen[-1].startswith("└")


def test_langer_link_im_status_wird_umbrochen_nicht_abgeschnitten(mon):
    link = "https://idp.example/device?user_code=" + "A" * 150
    mon.log(f"Anmeldung nötig — öffne {link}")
    zeilen = screen.render(mon.snapshot(), HEADER, 80, 24)
    status = next(i for i, z in enumerate(zeilen) if "Status" in z)
    zusammen = "".join(z[2:-1].rstrip() for z in zeilen[status + 1 : -1])
    assert link in zusammen


def test_zu_kleines_fenster_sagt_es(mon):
    zeilen = screen.render(mon.snapshot(), HEADER, 40, 10)
    assert len(zeilen) == 10
    assert "zu klein" in zeilen[0]


# --- Hülle --------------------------------------------------------------------


def test_ohne_konsole_uebernimmt_die_anzeige_nichts(mon):
    ausgabe = io.StringIO()
    anzeige = screen.Screen(mon, lambda: HEADER, stream=ausgabe)
    grund = anzeige.start()
    anzeige.stop()
    assert grund == "die Ausgabe ist kein Konsolenfenster"
    assert ausgabe.getvalue() == ""


def test_zeichnen_und_zurueckgeben(mon, monkeypatch):
    class Konsole(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(screen.os, "name", "posix")
    ausgabe = Konsole()
    anzeige = screen.Screen(mon, lambda: HEADER, stream=ausgabe)
    assert anzeige.start() is None
    threading.Event().wait(0.1)
    anzeige.stop()
    text = ausgabe.getvalue()
    assert text.startswith("\x1b[?1049h")
    assert "botproxy" in text
    # The window goes back the way it was: wrap on, cursor on, normal buffer.
    assert text.endswith("\x1b[?7h\x1b[?25h\x1b[?1049l")


# --- Verdrahtung --------------------------------------------------------------


def test_verkehr_durch_den_port_erscheint_in_der_anzeige(proxy, upstream):
    upstream.chunks = [b"data: a\n\n", b"data: b\n\n"]
    status, _ = ruf(proxy, "/v1/chat/completions", key="geheim-fuer-den-test")
    ruf(proxy, "/v1/chat/completions", key="falsch")

    snap = proxy.monitor.snapshot()
    assert status == 200
    assert snap.total == 2
    assert snap.running == 0
    assert "200 fertig" in snap.answers[0]
    assert "2 Stücken" in snap.answers[0]
    assert snap.answers[1].endswith("403 forbidden_key")


def test_in_der_anzeige_steht_kein_geheimnis(proxy, upstream):
    """Token, lokaler Key und Body kommen nie beim Monitor an."""
    token = proxy.manager.ensure_fresh()
    upstream.chunks = [b"data: antwort-inhalt\n\n"]
    ruf(
        proxy,
        "/v1/chat/completions",
        key="geheim-fuer-den-test",
        body=b'{"prompt": "quelltext-des-benutzers"}',
    )
    proxy.monitor.log("eine Meldung")

    snap = proxy.monitor.snapshot()
    alles = "\n".join(snap.requests + snap.answers + snap.log)
    alles += "\n".join(screen.render(snap, HEADER, 200, 40))
    for geheim in (token, "geheim-fuer-den-test", "quelltext", "antwort-inhalt"):
        assert geheim not in alles


def test_say_geht_bei_laufender_anzeige_in_den_status(monkeypatch, capsys, mon):
    monkeypatch.setattr(server, "_sink", mon.log)
    server.say("Token erneuert, gültig bis 21:14")
    assert capsys.readouterr().err == ""
    assert mon.snapshot().log[-1].endswith("Token erneuert, gültig bis 21:14")
