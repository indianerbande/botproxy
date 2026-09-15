"""Einsprung. `serve` ist die Vorgabe, `status` fragt eine laufende Instanz."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from botproxy import config
from botproxy.errors import BotproxyError


def status() -> int:
    url = f"http://127.0.0.1:{config.PORT}/_botproxy/status"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        print(
            f"Auf Port {config.PORT} antwortet kein botproxy.",
            file=sys.stderr,
        )
        return 1
    print(f"Zustand:  {payload.get('zustand')}")
    print(f"Endpunkt: {payload.get('endpunkt')}")
    if payload.get("gueltig_bis"):
        print(f"Token:    gültig bis {payload['gueltig_bis']}")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "status":
        return status()
    if command not in ("serve", ""):
        print(f"Unbekannter Befehl: {command}. Bekannt: serve, status.")
        return 2
    # Imported here so `status` works without a configured target — it only
    # needs the port, and a missing BASE_URL must not stop a status query.
    from botproxy import server

    try:
        return server.serve()
    except BotproxyError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
