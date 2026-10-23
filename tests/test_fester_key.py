"""Fester Key: keine Anmeldung, kein Erneuern, und der Key bleibt unsichtbar."""

from __future__ import annotations

import json
import threading
import time

import pytest

from botproxy import config, forward, server, store, tokens
from tests.stubs import make_jwt
from tests.test_server import ruf

KEY = "fester-test-key"


@pytest.fixture
def fest(settings, monkeypatch):
    """A manager running on a fixed key, with a notify that keeps every message."""
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", KEY)
    meldungen: list[str] = []
    manager = tokens.Manager(notify=meldungen.append)
    manager.meldungen = meldungen  # type: ignore[attr-defined]
    yield manager
    manager.stop()


@pytest.fixture
def fest_proxy(fest, monkeypatch):
    monkeypatch.setattr(config, "PORT", 0)
    instance = server.Proxy(fest, "geheim-fuer-den-test")
    monkeypatch.setattr(config, "PORT", instance.server_address[1])
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.forwarder.close()
    instance.server_close()


def senden(manager, headers=None):
    f = forward.Forwarder(manager)
    try:
        relayed = f.send("POST", "/v1/chat/completions", "", headers or [], b"{}")
        status = relayed.status
        relayed.close()
    finally:
        f.close()
    return status


# --- Token-Verwaltung --------------------------------------------------------


def test_fester_key_ohne_anmeldung_und_ohne_erneuerung(fest, idp):
    for _ in range(3):
        assert fest.ensure_fresh() == KEY
    assert not fest.renewable
    assert idp.device_requests == 0
    assert idp.refreshes == 0


def test_fester_key_wird_nicht_auf_die_platte_geschrieben(fest):
    """It already lives where the user put it; a second copy is one more to find."""
    fest.ensure_fresh()
    assert not config.TOKEN_FILE.exists()
    assert not config.REFRESH_FILE.exists()


def test_altes_token_auf_der_platte_wird_nicht_benutzt(settings, monkeypatch):
    """Switched over from sign-in: the leftover token must not win."""
    alt = make_jwt(3600)
    store.write_secret(config.TOKEN_FILE, alt)
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", KEY)
    manager = tokens.Manager()
    try:
        assert manager.ensure_fresh() == KEY
    finally:
        manager.stop()
    assert store.read_token(config.TOKEN_FILE) == alt


def test_wecker_startet_nicht(fest, idp):
    fest.start_watchdog()
    time.sleep(config.CHECK_INTERVAL_SECONDS * 1.5)
    assert fest._watchdog is None
    assert idp.refreshes == 0
    assert idp.device_requests == 0


def test_auskunft_nennt_den_festen_key_nicht(fest):
    snapshot = fest.snapshot()
    assert snapshot["zustand"] == "ok"
    assert snapshot["zugang"] == "fester_key"
    assert KEY not in json.dumps(snapshot)
    assert KEY not in fest.describe_validity()
    assert "fester Key" in fest.describe_validity()


# --- Weiterreichen -----------------------------------------------------------


def test_fester_key_ersetzt_den_lokalen(fest, upstream):
    senden(fest, [("Authorization", "Bearer lokaler-key")])
    assert upstream.seen[-1]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_fester_key_in_eigener_kopfzeile(fest, upstream, monkeypatch):
    """Some endpoints want `api-key: …`. Then no Authorization goes out at all —
    the client's local key must not reach the endpoint under any name."""
    monkeypatch.setattr(config, "UPSTREAM_API_KEY_HEADER", "api-key")
    senden(fest, [("Authorization", "Bearer lokaler-key"), ("api-key", "vom-client")])

    headers = {k.lower(): v for k, v in upstream.seen[-1]["headers"].items()}
    assert headers["api-key"] == KEY
    assert "authorization" not in headers


def test_abgelehnter_fester_key_wird_nicht_wiederholt(fest, upstream, idp):
    """A fixed key does not get better by asking again: one request, the 401
    goes through, and the window says once what to check."""
    upstream.reject_next = 5
    assert senden(fest) == 401
    assert senden(fest) == 401

    assert len(upstream.seen) == 2
    assert idp.refreshes == 0
    assert idp.device_requests == 0
    hinweise = [m for m in fest.meldungen if "BOTPROXY_UPSTREAM_API_KEY" in m]
    assert len(hinweise) == 1
    assert KEY not in " ".join(fest.meldungen)


# --- Port und Ausgabe --------------------------------------------------------


def test_status_meldet_festen_key_ohne_wert(fest_proxy):
    status, body = ruf(fest_proxy, "/_botproxy/status", method="GET", body=None)
    assert status == 200
    payload = json.loads(body)
    assert payload["zugang"] == "fester_key"
    assert KEY not in body.decode()


def test_kein_fester_key_in_der_ausgabe(fest_proxy, upstream, capsys):
    upstream.reject_next = 1
    ruf(fest_proxy, "/v1/chat/completions", key="geheim-fuer-den-test")
    server._plain_banner(fest_proxy.manager, fest_proxy, "geheim-fuer-den-test")
    kopf = server._header(fest_proxy.manager, fest_proxy, "geheim-fuer-den-test")

    ausgabe = capsys.readouterr()
    gesamt = ausgabe.out + ausgabe.err + kopf.route + kopf.state + kopf.client
    assert KEY not in gesamt
    assert "fester Key" in kopf.state
