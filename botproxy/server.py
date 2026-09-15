"""Der Port, die Routen, und was davon sichtbar wird.

Here it is decided what becomes visible. Everything else reports through
return values, exceptions and `say`; `screen.py` only draws what this module
hands it — traffic figures from the monitor, never a header value or a body.
"""

from __future__ import annotations

import hmac
import json
import socket
import sys
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from botproxy import config, forward, monitor, screen, tokens
from botproxy.errors import UpstreamError

MAX_BODY_BYTES = 64 * 1024 * 1024

# A client closing its end. ConnectionAbortedError is how Windows reports it
# when its own side gave up the connection.
_CLIENT_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


# Where `say` goes while the full-screen display owns the window. Printing
# then would tear through the drawn areas.
_sink: Callable[[str], None] | None = None


def say(message: str) -> None:
    if _sink is not None:
        _sink(message)
        return
    print(message, file=sys.stderr, flush=True)


def _error_body(message: str, code: str) -> bytes:
    """A failure in the shape an OpenAI client understands.

    Clients render `message` in their own interface — which is where the user is
    looking, and not a terminal window behind it.
    """
    return json.dumps(
        {"error": {"message": message, "type": "botproxy", "code": code}}
    ).encode()


class Handler(BaseHTTPRequestHandler):
    """One request. The server instance carries manager and forwarder."""

    protocol_version = "HTTP/1.1"
    server_version = "botproxy"
    sys_version = ""
    # The monitor's number for the request in progress on this connection.
    _rid: int | None = None

    # --- Eingang -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — name fixed by the base class
        if self.path.startswith("/_botproxy/status"):
            self._send_status()
            return
        self._relay("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._relay("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._relay("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS, ever. What no browser should reach gets no permission slip.
        self._fail(405, "OPTIONS wird nicht beantwortet.", "no_cors")

    def log_message(self, format: str, *args: object) -> None:
        """Quiet by default — one line per request would drown the status.

        Never log headers or bodies: the first carries the token, the second
        the user's source code.
        """
        return

    # --- Prüfungen -----------------------------------------------------------

    def _from_browser(self) -> bool:
        """Any page open in a browser can reach 127.0.0.1.

        Both headers are set by browsers themselves and by no ordinary HTTP
        client, which makes them a reliable tell — and one the page cannot
        suppress.
        """
        return bool(self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"))

    def _key_ok(self) -> bool:
        presented = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not presented.startswith(prefix):
            return False
        return hmac.compare_digest(presented[len(prefix) :], self.server.local_key)

    # --- Antworten -----------------------------------------------------------

    def _fail(self, status: int, message: str, code: str) -> None:
        """Refuse, and end the connection with the answer.

        Most refusals come before the request body has been read. On a kept-alive
        connection those unread bytes would be taken for the next request — so
        the connection closes instead of guessing where the body ends.
        """
        if self._rid is not None:
            self.server.monitor.rejected(self._rid, status, code)
            self._rid = None
        body = _error_body(message, code)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_status(self) -> None:
        payload = dict(self.server.manager.snapshot())
        payload["port"] = config.PORT
        payload["endpunkt"] = self.server.upstream_state
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- Weiterreichen -------------------------------------------------------

    def _relay(self, method: str) -> None:
        declared = self.headers.get("Content-Length", "").strip()
        self._rid = self.server.monitor.request(
            method,
            self.path,
            int(declared) if declared.isascii() and declared.isdigit() else None,
        )
        try:
            self._relay_checked(method)
        finally:
            # Left set only when something raised on the way: without this the
            # answer would say "wartet" until the window is closed.
            if self._rid is not None:
                self.server.monitor.aborted(self._rid, "Fehler")
                self._rid = None

    def _relay_checked(self, method: str) -> None:
        if not self.path.startswith("/v1"):
            self._fail(404, f"Unbekannter Pfad: {self.path}", "not_found")
            return
        if self._from_browser():
            self._fail(
                403,
                "Anfragen aus einem Browser werden nicht bedient.",
                "forbidden_origin",
            )
            return
        if not self._key_ok():
            self._fail(
                403,
                "Falscher oder fehlender API-Key. Er steht in der Statuszeile "
                "von botproxy und gehört ins API-Key-Feld des Clients.",
                "forbidden_key",
            )
            return

        length = self._body_length()
        if length is None:
            return
        if length > MAX_BODY_BYTES:
            self._fail(413, "Anfrage zu groß.", "too_large")
            return
        # Read in full: the 401 retry needs to send the same bytes twice, and a
        # stream cannot be replayed. Chat requests are finite JSON.
        body = self.rfile.read(length) if length else b""
        path, _, query = self.path.partition("?")
        self._pump(method, path, query, body)

    def _body_length(self) -> int | None:
        """How many body bytes follow, or None after refusing the request.

        The body is read in full before forwarding, and that needs its length
        up front. Without one, reading nothing would send an empty body on —
        the endpoint would answer something about a missing field, and nothing
        would point at the transfer.
        """
        if self.headers.get("Transfer-Encoding"):
            # Also when Content-Length is present: with both, the length is to
            # be ignored (RFC 9112 §6.3), so it says nothing reliable.
            self._fail(
                411,
                "Anfragen ohne Content-Length werden nicht angenommen. botproxy "
                "braucht den Body vollständig, um ihn nach einem 401 erneut "
                "senden zu können; Transfer-Encoding wird nicht gelesen.",
                "length_required",
            )
            return None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        # isascii: isdigit alone lets "²" through, which int() then rejects.
        if not (raw.isascii() and raw.isdigit()):
            self._fail(400, f"Ungültige Content-Length: {raw!r}", "bad_length")
            return None
        return int(raw)

    def _pump(self, method: str, path: str, query: str, body: bytes) -> None:
        try:
            relayed = self.server.forwarder.send(
                method, path, query, list(self.headers.items()), body
            )
        except tokens.LoginRequired as exc:
            self._fail(503, str(exc), "login_required")
            return
        except UpstreamError as exc:
            self._fail(502, str(exc), "upstream_unreachable")
            return
        except Exception as exc:  # noqa: BLE001 — a thread must not die silently
            self._fail(500, f"botproxy: {exc}", "internal")
            return

        watch = self.server.monitor
        rid, self._rid = self._rid, None
        if rid is not None:
            watch.answering(rid, relayed.status)
        outcome = "Fehler"
        try:
            self.send_response(relayed.status)
            for key, value in relayed.headers:
                self.send_header(key, value)
            # Chunked out. A set length would force collecting, and a streamed
            # answer would arrive in one block instead of word by word.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in relayed.body:
                if not chunk:
                    continue
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                if rid is not None:
                    watch.chunk(rid, len(chunk))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            outcome = ""
        except _CLIENT_GONE:
            # The client hung up mid-answer. Its right; nothing to report.
            outcome = "Client weg"
        finally:
            relayed.close()
            if rid is not None:
                if outcome:
                    watch.aborted(rid, outcome)
                else:
                    watch.finished(rid)


class Proxy(ThreadingHTTPServer):
    """Bound to the loopback interface. Never to 0.0.0.0, not even by switch."""

    daemon_threads = True
    # On Windows SO_REUSEADDR does not mean "past TIME_WAIT" but "share a port
    # someone is listening on". A second instance would bind without complaint,
    # and so would any other local process — which then receives the client's
    # requests, local key included.
    allow_reuse_address = False

    def __init__(self, manager: tokens.Manager, local_key: str) -> None:
        super().__init__(("127.0.0.1", config.PORT), Handler)
        self.manager = manager
        self.local_key = local_key
        self.forwarder = forward.Forwarder(manager)
        self.upstream_state = "ungeprüft"
        self.monitor = monitor.Monitor()

    def server_bind(self) -> None:
        """Claim the port exclusively, so nobody can bind it alongside us.

        Leaving out SO_REUSEADDR is not enough on Windows: a later socket that
        sets it may still share the port. SO_EXCLUSIVEADDRUSE forbids that.
        """
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request: object, client_address: object) -> None:
        """One line for a real failure, nothing for a client that hung up.

        The default prints a full traceback. Editor clients drop idle kept-alive
        connections all the time, and each drop would bury the status line
        under a stack trace that says nothing but "the other side left".
        """
        exc = sys.exception()
        if isinstance(exc, _CLIENT_GONE):
            return
        say(f"Fehler bei einer Anfrage: {type(exc).__name__}: {exc}")

    def probe(self) -> None:
        """Ask the endpoint for its models — the one honest test of the chain.

        A failure here is not fatal. The port is up and the sign-in worked;
        only the endpoint is quiet. A proxy that dies of this forces a restart
        every time the network blinks.
        """
        try:
            relayed = self.forwarder.send("GET", "/v1/models", "", [], b"")
        except Exception as exc:  # noqa: BLE001
            grund = str(exc).removeprefix("Endpunkt nicht erreichbar: ")
            self.upstream_state = f"nicht erreichbar ({grund})"
            return
        try:
            payload = b"".join(relayed.body)
        finally:
            relayed.close()
        if relayed.status != 200:
            self.upstream_state = f"antwortet mit HTTP {relayed.status}"
            return
        try:
            count = len(json.loads(payload).get("data", []))
        except ValueError:
            self.upstream_state = "antwortet, aber nicht im erwarteten Format"
            return
        self.upstream_state = f"antwortet, {count} Modelle"


def serve() -> int:
    """Start up, report, then bedienen. Returns the process exit code."""
    global _sink
    config.validate()
    manager = tokens.Manager(notify=say)
    key = config.local_key()

    # Sign in now if there is nothing usable — before the port promises
    # something it cannot keep.
    try:
        manager.ensure_fresh()
    except tokens.LoginRequired:
        say("Warte auf die Bestätigung im Browser …")
        try:
            signed_in = manager.wait_for_login()
        except KeyboardInterrupt:
            manager.stop()
            say("Beendet.")
            return 0
        if not signed_in:
            # The reason is already on screen, from the sign-in itself. A port
            # without a token would only turn it into a 503 in the client.
            say("Ohne Anmeldung startet botproxy nicht. Zum Wiederholen neu starten.")
            return 1

    try:
        proxy = Proxy(manager, key)
    except OSError as exc:
        say(
            f"Port {config.PORT} lässt sich nicht öffnen: {exc}. "
            "Vermutlich läuft schon eine Instanz — sonst BOTPROXY_PORT ändern."
        )
        return 1

    proxy.probe()
    manager.start_watchdog()
    display = screen.Screen(proxy.monitor, lambda: _header(manager, proxy, key))
    grund = display.start()
    if grund is None:
        _sink = proxy.monitor.log
        say(f"botproxy aktiv, Endpunkt {proxy.upstream_state}")
    else:
        _plain_banner(manager, proxy, key)
        say(f"Einfache Ausgabe ohne Bereiche, weil {grund}.")
        say("")
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        display.stop()
        _sink = None
        manager.stop()
        proxy.forwarder.close()
    say("Beendet.")
    return 0


def _header(manager: tokens.Manager, proxy: Proxy, key: str) -> screen.Header:
    base = f"http://127.0.0.1:{config.PORT}"
    if manager.snapshot()["zustand"] == "anmeldung_läuft":
        token = "Anmeldung läuft — Link und Code unter Status"
    else:
        token = f"Token {manager.describe_validity()}"
    return screen.Header(
        route=f"{base}  →  {config.BASE_URL}",
        state=f"{token} · Endpunkt {proxy.upstream_state}",
        client=f"Client: Base URL {base}/v1 · API-Key {key}",
    )


def _plain_banner(manager: tokens.Manager, proxy: Proxy, key: str) -> None:
    say("")
    say(f"botproxy aktiv auf http://127.0.0.1:{config.PORT}")
    say(f"Endpunkt {proxy.upstream_state} · Token {manager.describe_validity()}")
    say("")
    say(f"Client — Base URL: http://127.0.0.1:{config.PORT}/v1")
    say(f"Client — API-Key:  {key}")
