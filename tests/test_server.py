"""Der Port: wer durchkommt, wer nicht, und was nach außen dringt."""

from __future__ import annotations

import contextlib
import json
import socket
import struct
import threading
import urllib.error
import urllib.request

import pytest

from botproxy import server


def ruf(instance, pfad, *, key=None, extra=None, method="POST", body=b"{}"):
    url = f"http://127.0.0.1:{instance.server_address[1]}{pfad}"
    request = urllib.request.Request(url, data=body, method=method)
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    for name, value in (extra or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as fehler:
        return fehler.code, fehler.read()


def test_ohne_key_kein_durchkommen(proxy, upstream):
    status, _ = ruf(proxy, "/v1/chat/completions")
    assert status == 403
    # Nothing reached the endpoint: refusal happens before forwarding.
    assert upstream.seen == []


def test_falscher_key_kein_durchkommen(proxy, upstream):
    status, body = ruf(proxy, "/v1/chat/completions", key="geraten")
    assert status == 403
    assert json.loads(body)["error"]["code"] == "forbidden_key"
    assert upstream.seen == []


def test_richtiger_key_kommt_durch(proxy, upstream):
    status, _ = ruf(proxy, "/v1/chat/completions", key="geheim-fuer-den-test")
    assert status == 200
    assert len(upstream.seen) == 1


@pytest.mark.parametrize(
    "header",
    [{"Origin": "https://irgendwas.example"}, {"Sec-Fetch-Site": "cross-site"}],
)
def test_browser_kommt_auch_mit_key_nicht_durch(proxy, upstream, header):
    """A web page that learned the key must still not get through."""
    status, body = ruf(
        proxy, "/v1/chat/completions", key="geheim-fuer-den-test", extra=header
    )
    assert status == 403
    assert json.loads(body)["error"]["code"] == "forbidden_origin"
    assert upstream.seen == []


def test_fremder_pfad_ergibt_404(proxy):
    status, _ = ruf(proxy, "/admin", key="geheim-fuer-den-test")
    assert status == 404


def test_options_wird_nicht_beantwortet(proxy):
    """No CORS preflight, so no permission slip for a browser."""
    status, _ = ruf(proxy, "/v1/chat/completions", method="OPTIONS", body=None)
    assert status == 405


def test_status_braucht_keinen_key_und_verraet_kein_token(proxy):
    status, body = ruf(proxy, "/_botproxy/status", method="GET", body=None)
    assert status == 200

    payload = json.loads(body)
    assert payload["zustand"] == "ok"
    assert payload["restlaufzeit_sekunden"] > 0

    # Not the token, not a masked version of it, not a fragment.
    roh = body.decode()
    echtes = proxy.manager.ensure_fresh()
    assert echtes not in roh
    assert echtes[:12] not in roh
    assert "header." not in roh


def test_probe_erkennt_die_modelle(proxy):
    proxy.probe()
    assert "2 Modelle" in proxy.upstream_state


def test_kein_token_in_der_ausgabe(proxy, capsys, monkeypatch):
    """Whatever botproxy prints, it is never the credential.

    Checked by running a request with logging as loud as it gets and then
    searching the captured output for the token itself.
    """
    echtes = proxy.manager.ensure_fresh()
    ruf(proxy, "/v1/chat/completions", key="geheim-fuer-den-test")
    server.say("eine gewöhnliche Meldung")

    ausgabe = capsys.readouterr()
    gesamt = ausgabe.out + ausgabe.err
    assert echtes not in gesamt
    assert "geheim-fuer-den-test" not in gesamt


def test_streaming_geht_gestueckelt_durch_den_port(proxy, upstream):
    """End to end: chunked in, chunked out, nothing collected in between."""
    upstream.chunks = [b"data: a\n\n", b"data: b\n\n", b"data: [DONE]\n\n"]
    upstream.chunk_delay = 0.2

    url = f"http://127.0.0.1:{proxy.server_address[1]}/v1/chat/completions"
    request = urllib.request.Request(url, data=b"{}", method="POST")
    request.add_header("Authorization", "Bearer geheim-fuer-den-test")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.headers.get("Transfer-Encoding") == "chunked"
        assert response.headers.get("Content-Length") is None
        stuecke = [zeile for zeile in response if zeile.strip()]

    assert len(stuecke) == 3


def test_start_ohne_bestaetigung_endet_mit_grund(idp, capsys):
    """An unconfirmed code at startup ends the start when it runs out.

    Not a quarter of an hour later, and not with a port that has no token
    behind it — that would only show up later as a 503 in the client.
    """
    idp.pending_polls = 1000
    idp.code_ttl = 2

    assert server.serve() == 1

    meldungen = capsys.readouterr().err
    assert "abgelaufen" in meldungen
    assert "startet botproxy nicht" in meldungen
    assert "aktiv auf" not in meldungen


def roh(instance, anfrage: bytes) -> bytes:
    """Bytes on a socket, exactly as written — no client adding its own headers."""
    with socket.create_connection(("127.0.0.1", instance.server_address[1])) as s:
        s.settimeout(5)
        s.sendall(anfrage)
        antwort = b""
        while chunk := s.recv(65536):
            antwort += chunk
    return antwort


KEY = b"Authorization: Bearer geheim-fuer-den-test\r\n"


def test_chunked_anfrage_ergibt_411(proxy, upstream):
    """Without a length nothing would be read, and an empty body would go on."""
    antwort = roh(
        proxy,
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
        + KEY
        + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
    )

    assert antwort.startswith(b"HTTP/1.1 411")
    assert b"length_required" in antwort
    assert upstream.seen == []


def test_transfer_encoding_schlaegt_content_length(proxy, upstream):
    antwort = roh(
        proxy,
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
        + KEY
        + b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"2\r\n{}\r\n0\r\n\r\n",
    )

    assert antwort.startswith(b"HTTP/1.1 411")
    assert upstream.seen == []


def test_ungueltige_content_length_ergibt_400(proxy, upstream):
    antwort = roh(
        proxy,
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
        + KEY
        + b"Content-Length: zwei\r\n\r\n{}",
    )

    assert antwort.startswith(b"HTTP/1.1 400")
    assert b"bad_length" in antwort
    assert upstream.seen == []


def test_ungelesener_body_wird_nicht_zur_naechsten_anfrage(proxy, upstream):
    """A refusal leaves the body unread. On a kept-alive connection those bytes
    would be parsed as the next request — here one that carries a valid key."""
    versteckt = b"GET /v1/models HTTP/1.1\r\nHost: x\r\n" + KEY + b"\r\n"
    antwort = roh(
        proxy,
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
        + b"Content-Length: %d\r\n\r\n" % len(versteckt)
        + versteckt,
    )

    assert antwort.startswith(b"HTTP/1.1 403")
    assert antwort.count(b"HTTP/1.1 ") == 1
    assert upstream.seen == []


def test_zweite_instanz_bekommt_den_port_nicht(proxy, signed_in):
    """The start message says "vermutlich läuft schon eine Instanz" — which only
    helps if binding actually fails. On Windows SO_REUSEADDR would let it pass."""
    with pytest.raises(OSError):
        server.Proxy(signed_in, "zweiter-key")


def test_aufgelegter_client_hinterlaesst_keinen_traceback(proxy, capsys):
    """A client resetting an idle kept-alive connection is routine, not news."""
    port = proxy.server_address[1]
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(5)
    s.sendall(b"GET /_botproxy/status HTTP/1.1\r\nHost: x\r\n\r\n")
    antwort = b""
    while b"}" not in antwort:
        antwort += s.recv(65536)
    # Linger 0: close sends RST instead of FIN, as a dropped pooled connection does.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()
    threading.Event().wait(0.5)

    fehler = capsys.readouterr().err
    assert "Traceback" not in fehler
    assert "ConnectionResetError" not in fehler


def test_echter_fehler_ist_eine_zeile(proxy, capsys, monkeypatch):
    def kaputt(self):
        raise ValueError("etwas ging schief")

    monkeypatch.setattr(server.Handler, "do_GET", kaputt)
    with contextlib.suppress(OSError):
        ruf(proxy, "/v1/models", method="GET", body=None)
    threading.Event().wait(0.5)

    fehler = capsys.readouterr().err
    assert "ValueError: etwas ging schief" in fehler
    assert "Traceback" not in fehler
