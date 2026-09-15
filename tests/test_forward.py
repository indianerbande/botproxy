"""Was durchgereicht wird — und was unterwegs verschwinden muss."""

from __future__ import annotations

import json
import time

import pytest

from botproxy import forward
from botproxy.errors import UpstreamError


def test_pfad_query_und_body_kommen_unveraendert_an(signed_in, upstream):
    body = json.dumps({"model": "x", "feld_das_es_noch_nicht_gibt": 42}).encode()
    f = forward.Forwarder(signed_in)
    relayed = f.send(
        "POST",
        "/v1/chat/completions",
        "beta=1",
        [("Content-Type", "application/json")],
        body,
    )
    relayed.close()
    f.close()

    seen = upstream.seen[-1]
    assert seen["path"] == "/v1/chat/completions?beta=1"
    # Byte-for-byte: the proxy must not normalise, reorder or re-encode.
    assert seen["body"] == body


def test_authorization_wird_ersetzt_nicht_ergaenzt(signed_in, upstream):
    f = forward.Forwarder(signed_in)
    relayed = f.send(
        "POST",
        "/v1/chat/completions",
        "",
        [("Authorization", "Bearer lokaler-dummy-key")],
        b"{}",
    )
    relayed.close()
    f.close()

    presented = upstream.seen[-1]["headers"]["Authorization"]
    assert presented != "Bearer lokaler-dummy-key"
    assert presented.startswith("Bearer header.")


def test_hop_by_hop_header_reisen_nicht_weiter(signed_in, upstream):
    f = forward.Forwarder(signed_in)
    relayed = f.send(
        "POST",
        "/v1/chat/completions",
        "",
        [
            ("Connection", "keep-alive, X-Eigen"),
            ("Keep-Alive", "timeout=5"),
            ("X-Eigen", "weg"),
            ("X-Bleibt", "da"),
        ],
        b"{}",
    )
    relayed.close()
    f.close()

    headers = upstream.seen[-1]["headers"]
    assert "Keep-Alive" not in headers
    # Named in `Connection`, so it is hop-by-hop for this connection only.
    assert "X-Eigen" not in headers
    assert headers["X-Bleibt"] == "da"


def test_streaming_kommt_in_stuecken_nicht_am_stueck(signed_in, upstream):
    """The point of the whole exercise: no buffering anywhere in between."""
    upstream.chunks = [b"data: eins\n\n", b"data: zwei\n\n", b"data: [DONE]\n\n"]
    upstream.chunk_delay = 0.2

    f = forward.Forwarder(signed_in)
    relayed = f.send("POST", "/v1/chat/completions", "", [], b"{}")
    start = time.monotonic()
    arrivals = []
    for chunk in relayed.body:
        if chunk.strip():
            arrivals.append((time.monotonic() - start, chunk))
    relayed.close()
    f.close()

    assert len(arrivals) >= 3
    # Had anything collected the answer, every piece would land at once.
    assert arrivals[-1][0] - arrivals[0][0] > 0.25


def test_content_length_faellt_weg(signed_in, upstream):
    f = forward.Forwarder(signed_in)
    relayed = f.send("GET", "/v1/models", "", [], b"")
    namen = {key.lower() for key, _ in relayed.headers}
    relayed.close()
    f.close()

    # A length would force collecting, and streaming would be lost silently.
    assert "content-length" not in namen


def test_401_fuehrt_zu_genau_einem_zweiten_versuch(signed_in, upstream, idp):
    upstream.reject_next = 1
    idp.rotate = False

    f = forward.Forwarder(signed_in)
    relayed = f.send("POST", "/v1/chat/completions", "", [], b"{}")
    relayed.close()
    f.close()

    assert relayed.status == 200
    assert len(upstream.seen) == 2
    # The retry must carry a token fetched after the refusal, not the old one.
    assert (
        upstream.seen[0]["headers"]["Authorization"]
        != (upstream.seen[1]["headers"]["Authorization"])
    )
    assert idp.refreshes == 1


def test_zweites_401_wird_durchgereicht(signed_in, upstream):
    """A 401 that survives a fresh token is the endpoint's verdict, not a bug."""
    upstream.reject_next = 2

    f = forward.Forwarder(signed_in)
    relayed = f.send("POST", "/v1/chat/completions", "", [], b"{}")
    relayed.close()
    f.close()

    assert relayed.status == 401
    assert len(upstream.seen) == 2


def test_unerreichbarer_endpunkt_wird_benannt(signed_in, monkeypatch):
    from botproxy import config

    monkeypatch.setattr(config, "BASE_URL", "http://127.0.0.1:1/v1")
    f = forward.Forwarder(signed_in)
    with pytest.raises(UpstreamError) as fehler:
        f.send("GET", "/v1/models", "", [], b"")
    f.close()

    # Not the outermost "Connection error." — the cause names what to fix.
    assert "Endpunkt nicht erreichbar" in str(fehler.value)
    assert str(fehler.value).strip() != "Endpunkt nicht erreichbar:"
