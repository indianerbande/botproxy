"""Der Port: wer durchkommt, wer nicht, und was nach außen dringt."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from botproxy import config, server


@pytest.fixture
def proxy(signed_in, monkeypatch):
    """A real proxy on a free port, with a known local key."""
    monkeypatch.setattr(config, "PORT", 0)
    instance = server.Proxy(signed_in, "geheim-fuer-den-test")
    monkeypatch.setattr(config, "PORT", instance.server_address[1])
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.forwarder.close()
    instance.server_close()


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
