"""Die Anfrage weiterreichen. Mehr nicht.

This module never parses the body. It does not know about models, prompts or
request schemas — everything it believed about the content would be wrong the
day the client changes its format. It knows an address and a token.
"""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import truststore

from botproxy import config, tokens
from botproxy.errors import UpstreamError

# Hop-by-hop headers (RFC 9110 §7.6.1). They describe one connection and mean
# nothing on the next one; passing them through produces subtle framing bugs.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Set by us, never copied.
REPLACED = frozenset({"host", "authorization", "content-length"})


def _client() -> httpx.Client:
    """An HTTP client that trusts what the operating system trusts.

    Behind a TLS-inspecting proxy, verification against the bundled `certifi`
    list fails every request — while the sign-in keeps working, because that
    goes through `urllib` and reads the system store. Same network, two
    verdicts, and the difference is invisible from the message alone.
    """
    return httpx.Client(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        timeout=httpx.Timeout(config.UPSTREAM_TIMEOUT_SECONDS, connect=15.0),
        follow_redirects=False,
    )


def clean_request_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Everything the client sent, minus what must not travel further."""
    listed = {
        name.strip().lower()
        for key, value in headers
        if key.lower() == "connection"
        for name in value.split(",")
    }
    return [
        (key, value)
        for key, value in headers
        if key.lower() not in HOP_BY_HOP
        and key.lower() not in REPLACED
        and key.lower() not in listed
    ]


def clean_response_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    """What goes back to the client.

    `Content-Length` is dropped on purpose: the answer leaves chunked. A set
    length forces collecting, and then a streamed answer arrives in one block
    instead of word by word — the effect would be lost silently.
    """
    return [
        (key, value)
        for key, value in headers.multi_items()
        if key.lower() not in HOP_BY_HOP and key.lower() != "content-length"
    ]


@dataclass
class Relayed:
    """An upstream answer, still open. `body` must be consumed, then `close()`."""

    status: int
    headers: list[tuple[str, str]]
    body: Iterator[bytes]
    _ctx: object

    def close(self) -> None:
        exit_ = getattr(self._ctx, "__exit__", None)
        if exit_ is not None:
            exit_(None, None, None)


class Forwarder:
    """Holds one HTTP client for the life of the process."""

    def __init__(self, manager: tokens.Manager) -> None:
        self._manager = manager
        self._client = _client()

    def close(self) -> None:
        self._client.close()

    def target(self, path: str, query: str) -> str:
        """`BASE_URL` plus whatever came after `/v1`, unchanged."""
        suffix = path[len("/v1") :] if path.startswith("/v1") else path
        url = f"{config.BASE_URL}{suffix}"
        return f"{url}?{query}" if query else url

    def send(
        self,
        method: str,
        path: str,
        query: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> Relayed:
        """Forward once, and a second time if the endpoint says 401.

        The retry is only safe here, before any byte has reached the client.
        Once the answer has started, nothing is repeated.
        """
        token = self._manager.ensure_fresh()
        relayed = self._attempt(method, path, query, headers, body, token)
        if relayed.status != 401:
            return relayed
        relayed.close()
        # The endpoint's opinion beats our arithmetic on `exp` — but only the
        # first of several refused requests needs to fetch a new token.
        token = self._manager.force_refresh(stale=token)
        return self._attempt(method, path, query, headers, body, token)

    def _attempt(
        self,
        method: str,
        path: str,
        query: str,
        headers: list[tuple[str, str]],
        body: bytes,
        token: str,
    ) -> Relayed:
        outgoing = clean_request_headers(headers)
        outgoing.append(("Authorization", f"Bearer {token}"))
        context = self._client.stream(
            method,
            self.target(path, query),
            headers=outgoing,
            content=body or None,
        )
        try:
            response = context.__enter__()
        except httpx.HTTPError as exc:
            raise UpstreamError(_reason(exc)) from exc
        return Relayed(
            status=response.status_code,
            headers=clean_response_headers(response.headers),
            body=response.iter_raw(),
            _ctx=context,
        )


_CERTIFICATE_MARKERS = (
    "certificate verify failed",
    "certificate_verify_failed",
    "unable to get local issuer",
    "self signed certificate",
    "self-signed certificate",
)


def _reason(exc: Exception) -> str:
    """The deepest cause, named — because the top of the chain says nothing.

    Reporting only the outermost exception turns a refused certificate, a
    blocked port and an unknown host into the same sentence, and each of them
    calls for something different.
    """
    seen: set[int] = set()
    current: BaseException = exc
    while current.__cause__ is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__
    text = str(current).strip() or type(current).__name__
    if any(marker in text.lower() for marker in _CERTIFICATE_MARKERS):
        return (
            f"Das Zertifikat des Endpunkts wurde abgelehnt ({text}). "
            "Prüfe, ob die ausstellende CA im Zertifikatsspeicher des Systems "
            "liegt."
        )
    return f"Endpunkt nicht erreichbar: {text}"
