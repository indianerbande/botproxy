"""Die Werte, mit denen botproxy arbeitet.

Read once at import. Everything comes from the environment; the mechanics of
reading and checking live in `_env.py` so the values stay at the top, where
someone opening this file expects them.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from botproxy import _env
from botproxy.errors import ConfigError

# --- Ziel --------------------------------------------------------------------
# Der Pfad gehört mit in die URL. Fehlt er, antwortet der Endpunkt mit 404.
BASE_URL: str = _env.string("BOTPROXY_BASE_URL")

# --- Anmeldung ---------------------------------------------------------------
AUTHORITY: str = _env.string("BOTPROXY_AUTHORITY")
CLIENT_ID: str = _env.string("BOTPROXY_CLIENT_ID")

# Ohne offline_access gibt der Provider kein Refresh-Token aus, und Erneuern hieße
# jedes Mal wieder Anmelden.
SCOPES: str = _env.string("BOTPROXY_SCOPES", "openid offline_access")

# --- Betrieb -----------------------------------------------------------------
PORT: int = _env.integer("BOTPROXY_PORT", 8127)
REFRESH_MARGIN_SECONDS: int = _env.integer("BOTPROXY_REFRESH_MARGIN", 300)
CHECK_INTERVAL_SECONDS: int = _env.integer("BOTPROXY_CHECK_INTERVAL", 60)

# Wie lange auf den Endpunkt gewartet wird. Großzügig: ein Modell darf denken.
UPSTREAM_TIMEOUT_SECONDS: float = float(_env.integer("BOTPROXY_UPSTREAM_TIMEOUT", 600))

# --- Dateien -----------------------------------------------------------------
HOME: Path = Path(_env.string("BOTPROXY_HOME", str(Path.home() / ".botproxy")))
TOKEN_FILE: Path = HOME / "token"
REFRESH_FILE: Path = HOME / "refresh.json"
LOCAL_KEY_FILE: Path = HOME / "local_key"


def validate() -> None:
    """Alles prüfen, was den Start verhindern soll — und zwar jetzt.

    Called once before the port is opened. Every message names the variable,
    because that is the only thing the reader can act on.
    """
    _env.require_url("BOTPROXY_BASE_URL", BASE_URL)
    _env.require_url("BOTPROXY_AUTHORITY", AUTHORITY)
    _env.require_value("BOTPROXY_CLIENT_ID", CLIENT_ID)
    if "offline_access" not in SCOPES.split():
        raise ConfigError(
            "BOTPROXY_SCOPES enthält kein offline_access. Ohne diesen Scope "
            "gibt der Identity Provider kein Refresh-Token aus, und nach einer "
            "Stunde käme die Anmeldung wieder."
        )
    if not 1 <= PORT <= 65535:
        raise ConfigError(f"BOTPROXY_PORT liegt außerhalb 1-65535: {PORT}")
    if REFRESH_MARGIN_SECONDS <= CHECK_INTERVAL_SECONDS:
        raise ConfigError(
            "BOTPROXY_REFRESH_MARGIN muss größer sein als "
            "BOTPROXY_CHECK_INTERVAL, sonst kann der Wecker den Ablauf "
            "verschlafen."
        )


def local_key() -> str:
    """Das lokale Geheimnis, erzeugt beim ersten Aufruf.

    It exists because anything that reaches the port speaks to the model with
    the user's identity — including any web page open in a browser. Clients
    usually have an API key field that would otherwise stay empty; this goes in
    there.
    """
    if LOCAL_KEY_FILE.is_file():
        existing = LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    from botproxy import store

    key = secrets.token_urlsafe(32)
    store.write_secret(LOCAL_KEY_FILE, key)
    return key
