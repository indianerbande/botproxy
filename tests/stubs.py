"""Zwei Stubs, die im Testlauf selbst starten.

No docker, no fixtures from outside, no network. Each stub binds port 0 and
reports which one it got, so tests can run in parallel and on any machine.
"""

from __future__ import annotations

import base64
import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_counter = itertools.count(1)


def make_jwt(expires_in: float) -> str:
    """A token that is structurally a JWT and carries `exp` plus a serial.

    The signature is deliberately nonsense — nothing in botproxy verifies it,
    and a test that pretended otherwise would be testing the test. The serial
    exists because two tokens issued in the same second would otherwise be
    byte-identical, and a test asking "did the token change?" would fail on a
    renewal that worked.
    """
    payload = {"exp": int(time.time() + expires_in), "jti": next(_counter)}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"header.{raw.rstrip('=')}.signature"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Clients hang up mid-answer on purpose here. A traceback per occurrence
        # would bury the actual test output.
        return


class Stub:
    """A server on a random port, started and stopped by the test."""

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._server = _Server(("127.0.0.1", 0), handler)
        self._server.stub = self  # type: ignore[attr-defined]
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class UpstreamHandler(BaseHTTPRequestHandler):
    """Records what arrived and answers however the test told it to."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        return

    @property
    def stub(self) -> Upstream:
        return self.server.stub  # type: ignore[attr-defined]

    def _record(self, method: str) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.stub.seen.append(
            {
                "method": method,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        return body

    def do_GET(self):  # noqa: N802
        self._record("GET")
        if self.path.startswith("/v1/models"):
            self._json({"data": [{"id": "stub-a"}, {"id": "stub-b"}]})
            return
        self._answer()

    def do_POST(self):  # noqa: N802
        self._record("POST")
        self._answer()

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _answer(self) -> None:
        if self.stub.reject_next > 0:
            self.stub.reject_next -= 1
            body = b'{"error":{"message":"abgelaufen"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.stub.chunks:
            # Chunked, wie es ein echter SSE-Endpunkt tut. Ohne Rahmen und ohne
            # Laenge wuesste der Client nie, wann die Antwort zu Ende ist.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in self.stub.chunks:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                time.sleep(self.stub.chunk_delay)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        self._json({"ok": True})


class Upstream(Stub):
    """The endpoint behind the proxy."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        # How many requests to answer with 401 before behaving.
        self.reject_next = 0
        self.chunks: list[bytes] = []
        self.chunk_delay = 0.05
        super().__init__(UpstreamHandler)


class IdpHandler(BaseHTTPRequestHandler):
    """Device Code Flow and refresh, enough of it to be believable."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        return

    @property
    def stub(self) -> Idp:
        return self.server.stub  # type: ignore[attr-defined]

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        fields = dict(part.split("=", 1) for part in raw.split("&") if "=" in part)
        if self.path.endswith("/devicecode"):
            self.stub.device_requests += 1
            self._json(
                200,
                {
                    "device_code": "dev-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": f"{self.stub.base}/device",
                    "expires_in": self.stub.code_ttl,
                    "interval": 1,
                },
            )
            return
        self._token(fields)

    def _token(self, fields: dict[str, str]) -> None:
        grant = fields.get("grant_type", "")
        if grant.endswith("device_code"):
            self.stub.polls += 1
            if self.stub.polls <= self.stub.pending_polls:
                self._json(400, {"error": "authorization_pending"})
                return
            self._issue()
            return
        if grant == "refresh_token":
            self.stub.refreshes += 1
            if self.stub.refresh_dead:
                self._json(400, {"error": "invalid_grant"})
                return
            self._issue()
            return
        self._json(400, {"error": "unsupported_grant_type"})

    def _issue(self) -> None:
        self.stub.issued += 1
        payload = {
            "access_token": make_jwt(self.stub.token_ttl),
            "expires_in": int(self.stub.token_ttl),
        }
        if self.stub.rotate:
            payload["refresh_token"] = f"refresh-{self.stub.issued}"
        else:
            payload["refresh_token"] = "refresh-fest"
        self.stub.last_access = payload["access_token"]
        self._json(200, payload)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Idp(Stub):
    """The identity provider."""

    def __init__(self) -> None:
        self.device_requests = 0
        self.polls = 0
        self.refreshes = 0
        self.issued = 0
        # How many polls answer `authorization_pending` before the user
        # "confirms" — 0 means the very first poll succeeds.
        self.pending_polls = 0
        # How long a device code stays valid. Short, to watch one run out.
        self.code_ttl = 60
        self.refresh_dead = False
        self.rotate = True
        self.token_ttl = 3600.0
        self.last_access = ""
        super().__init__(IdpHandler)
