"""Die Werte, mit denen botproxy arbeitet.

Read once at import. Everything comes from the environment; the mechanics of
reading and checking live in `_env.py` so the values stay at the top, where
someone opening this file expects them.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from botproxy import _env
from botproxy.errors import ConfigError

# --- Ziel --------------------------------------------------------------------
# Der Pfad gehört mit in die URL. Fehlt er, antwortet der Endpunkt mit 404.
# Ein Schrägstrich am Ende fällt weg: Pfade werden mit führendem angehängt, und
# `…/v1//chat/completions` muss ein Endpunkt nicht als denselben Pfad lesen.
BASE_URL: str = _env.string("BOTPROXY_BASE_URL").rstrip("/")

# --- Anmeldung ---------------------------------------------------------------
AUTHORITY: str = _env.string("BOTPROXY_AUTHORITY").rstrip("/")
CLIENT_ID: str = _env.string("BOTPROXY_CLIENT_ID")

# Ohne offline_access gibt der Provider kein Refresh-Token aus, und Erneuern hieße
# jedes Mal wieder Anmelden. Ob er eines ausgibt, steht nicht in `scp` des
# Zugangstokens — dort kann offline_access fehlen, obwohl eines kam.
#
# allatclaims lässt die Claims der Anmeldung ins Zugangstoken durch. Fehlen sie,
# besteht das Token die Prüfung von Signatur und Aussteller und scheitert erst an
# der Berechtigung: 403 statt 401, und nichts deutet auf den Scope. Ein Provider,
# der den Scope nicht kennt, lehnt ihn bei der Anmeldung mit Namen ab — dann hier
# weglassen.
SCOPES: str = _env.string("BOTPROXY_SCOPES", "openid allatclaims offline_access")

# --- Fester Key (statt Anmeldung) --------------------------------------------
# Manche Endpunkte geben statt kurzlebiger Token einen festen Key aus. Ist er
# gesetzt, meldet sich botproxy nicht an und erneuert nichts; AUTHORITY und
# CLIENT_ID bleiben dann leer. Der Key ist ein Geheimnis wie ein Token: er wird
# nicht gespeichert, nicht angezeigt und nicht geloggt.
UPSTREAM_API_KEY: str = _env.key("BOTPROXY_UPSTREAM_API_KEY")

# Die meisten Endpunkte erwarten `Authorization: Bearer <Key>`. Andere wollen den
# Key bloß, unter eigenem Namen (`api-key`, `X-API-Key`) — dann ohne „Bearer“.
AUTHORIZATION = "Authorization"
UPSTREAM_API_KEY_HEADER: str = _env.string(
    "BOTPROXY_UPSTREAM_API_KEY_HEADER", AUTHORIZATION
)

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
    if UPSTREAM_API_KEY:
        _validate_fixed_key()
    else:
        _validate_sign_in()
    if not 1 <= PORT <= 65535:
        raise ConfigError(f"BOTPROXY_PORT liegt außerhalb 1-65535: {PORT}")


def _validate_sign_in() -> None:
    if UPSTREAM_API_KEY_HEADER.lower() != AUTHORIZATION.lower():
        raise ConfigError(
            "BOTPROXY_UPSTREAM_API_KEY_HEADER ist gesetzt, aber kein "
            "BOTPROXY_UPSTREAM_API_KEY. Die Kopfzeile gilt nur für einen festen Key."
        )
    _env.require_url("BOTPROXY_AUTHORITY", AUTHORITY)
    _env.require_value("BOTPROXY_CLIENT_ID", CLIENT_ID)
    if "offline_access" not in SCOPES.split():
        raise ConfigError(
            "BOTPROXY_SCOPES enthält kein offline_access. Ohne diesen Scope "
            "gibt der Identity Provider kein Refresh-Token aus, und nach einer "
            "Stunde käme die Anmeldung wieder."
        )
    if REFRESH_MARGIN_SECONDS <= CHECK_INTERVAL_SECONDS:
        raise ConfigError(
            "BOTPROXY_REFRESH_MARGIN muss größer sein als "
            "BOTPROXY_CHECK_INTERVAL, sonst kann der Wecker den Ablauf "
            "verschlafen."
        )


def _validate_fixed_key() -> None:
    """Fixed key instead of sign-in. Messages never repeat the key itself."""
    beide = [
        name
        for name, value in (
            ("BOTPROXY_AUTHORITY", AUTHORITY),
            ("BOTPROXY_CLIENT_ID", CLIENT_ID),
        )
        if value
    ]
    if beide:
        raise ConfigError(
            f"BOTPROXY_UPSTREAM_API_KEY und {' und '.join(beide)} sind gesetzt. "
            "Entweder fester Key oder Anmeldung: für den festen Key "
            "BOTPROXY_AUTHORITY und BOTPROXY_CLIENT_ID leer lassen (in "
            "start-botproxy.cmd die beiden Zeilen auskommentieren)."
        )
    if UPSTREAM_API_KEY == _env.KEY_PLACEHOLDER:
        raise ConfigError(
            f"BOTPROXY_UPSTREAM_API_KEY enthält noch einen Platzhalter: "
            f"{_env.KEY_PLACEHOLDER!r}. Trage den Key ein."
        )
    if (
        not re.fullmatch(r"[\x21-\x7e]+", UPSTREAM_API_KEY)
        or UPSTREAM_API_KEY[0] in "\"'"
        or UPSTREAM_API_KEY[-1] in "\"'"
    ):
        raise ConfigError(
            "BOTPROXY_UPSTREAM_API_KEY enthält Leerzeichen, Anführungszeichen "
            "oder Zeichen außerhalb von ASCII. Der Key gehört in eine Zeile, "
            "ohne Anführungszeichen."
        )
    if not re.fullmatch(r"[A-Za-z0-9-]+", UPSTREAM_API_KEY_HEADER):
        raise ConfigError(
            "BOTPROXY_UPSTREAM_API_KEY_HEADER ist kein gültiger Name einer "
            f"Kopfzeile: {UPSTREAM_API_KEY_HEADER!r}. Erlaubt sind Buchstaben, "
            "Ziffern und Bindestrich, z. B. Authorization oder api-key."
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
