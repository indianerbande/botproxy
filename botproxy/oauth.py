"""OAuth 2.0 gegen den Identity Provider: Device Code Flow und Refresh.

Der Weg für Umgebungen ohne Domänenticket: botproxy zeigt einen kurzen Code, der
Benutzer bestätigt ihn im Browser, botproxy fragt derweil den Token-Endpunkt.
Anders als Windows Integrated Authentication braucht das keinen
domänengebundenen Rechner und funktioniert damit auch im Container.

Nur die Standardbibliothek, damit hier keine Abhängigkeit dazukommt, die im
Zielumgebung erst freigegeben werden müsste.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from botproxy.errors import AuthError

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"

# Ohne diesen Scope gibt der Server kein Refresh-Token aus, und Erneuern
# hiesse jedes Mal wieder Anmelden.
OFFLINE_SCOPE = "offline_access"

# RFC 8628 §3.5: bei slow_down soll der Abstand wachsen, nicht bloß gehalten
# werden. Der Server sagt nicht um wie viel; fünf Sekunden sind die übliche Wahl.
SLOW_DOWN_STEP = 5


@dataclass(frozen=True)
class DeviceCode:
    """Was der Identity Provider auf die erste Anfrage zurückgibt."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    verification_uri_complete: str | None = None
    # Der Server formuliert die Anweisung oft selbst; brauchbar als Rueckfall.
    message: str | None = None


class SlowDown(Exception):  # noqa: N818
    """Der Server bittet um größere Abstände — kein Fehler, eine Auflage."""


def device_endpoint(authority: str) -> str:
    return f"{authority.rstrip('/')}/oauth2/devicecode"


def token_endpoint(authority: str) -> str:
    return f"{authority.rstrip('/')}/oauth2/token"


def _post(url: str, fields: dict[str, str], timeout: float) -> tuple[int, dict]:
    """Formular abschicken. Fehlerantworten des Servers sind Daten, keine Ausnahme."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        # 400 mit {"error": "authorization_pending"} ist der Normalfall beim
        # Pollen, nicht ein Ausfall.
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthError(f"{url} nicht erreichbar: {exc}") from exc


def _decode(raw: bytes) -> dict:
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def request_code(
    authority: str, client_id: str, scopes: str, *, timeout: float = 10.0
) -> DeviceCode:
    url = device_endpoint(authority)
    status, payload = _post(url, {"client_id": client_id, "scope": scopes}, timeout)
    if status != 200:
        grund = (
            payload.get("error_description") or payload.get("error") or "ohne Angabe"
        )
        raise AuthError(f"{url} lehnte die Anfrage ab (HTTP {status}): {grund}")
    # Manche Provider liefern beides: verification_uri (RFC 8628) und das
    # aeltere, nicht standardisierte verification_url. Wir nehmen, was da ist.
    verification = payload.get("verification_uri") or payload.get("verification_url")
    missing = [key for key in ("device_code", "user_code") if not payload.get(key)]
    if not verification:
        missing.append("verification_uri")
    if missing:
        raise AuthError(
            f"Antwort von {url} unvollständig, es fehlt: {', '.join(missing)}"
        )
    return DeviceCode(
        device_code=str(payload["device_code"]),
        user_code=str(payload["user_code"]),
        verification_uri=str(verification),
        verification_uri_complete=payload.get("verification_uri_complete"),
        expires_in=int(payload.get("expires_in", 300)),
        interval=max(1, int(payload.get("interval", 5))),
        message=payload.get("message"),
    )


@dataclass(frozen=True)
class Tokens:
    """Was am Ende eines Grants herauskommt.

    `refresh` fehlt, wenn der Server keins ausgibt — etwa weil `offline_access`
    nicht angefordert oder nicht bewilligt wurde. Das ist kein Fehler, es
    bedeutet nur: Erneuern heisst dann wieder Anmelden.
    """

    access: str
    refresh: str | None = None
    expires_in: int | None = None


def _tokens_from(payload: dict, url: str) -> Tokens:
    access = payload.get("access_token")
    if not access:
        raise AuthError(f"{url} antwortete ohne access_token.")
    expires = payload.get("expires_in")
    return Tokens(
        access=str(access),
        refresh=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
        expires_in=int(expires) if isinstance(expires, int | float) else None,
    )


def refresh(
    authority: str,
    client_id: str,
    refresh_token: str,
    scopes: str,
    *,
    timeout: float = 10.0,
) -> Tokens:
    """Ein neues Zugangstoken, ohne den Benutzer erneut zu behelligen.

    Der Server darf dabei auch das Refresh-Token austauschen (Rotation). Wer
    das alte weiterverwendet, steht beim übernächsten Mal ohne da — deshalb
    gibt `Tokens.refresh` zurück, was tatsächlich zu speichern ist.
    """
    url = token_endpoint(authority)
    status, payload = _post(
        url,
        {
            "grant_type": REFRESH_GRANT,
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": scopes,
        },
        timeout,
    )
    if status == 200:
        return _tokens_from(payload, url)

    error = str(payload.get("error", ""))
    if error in ("invalid_grant", "expired_token"):
        raise AuthError(
            "Das Refresh-Token wird nicht mehr akzeptiert — /login holt ein neues."
        )
    grund = payload.get("error_description") or error or "ohne Angabe"
    raise AuthError(f"{url} lehnte die Erneuerung ab (HTTP {status}): {grund}")


def poll(
    authority: str, client_id: str, device_code: str, *, timeout: float = 10.0
) -> Tokens | None:
    """Das Token, oder None solange der Benutzer noch nicht bestätigt hat.

    Raises `SlowDown`, wenn der Server größere Abstände verlangt, und
    `AuthError` bei allem, was den Ablauf beendet.
    """
    url = token_endpoint(authority)
    status, payload = _post(
        url,
        {
            "grant_type": DEVICE_GRANT,
            "client_id": client_id,
            "device_code": device_code,
        },
        timeout,
    )
    if status == 200:
        return _tokens_from(payload, url)

    error = str(payload.get("error", ""))
    if error == "authorization_pending":
        return None
    if error == "slow_down":
        raise SlowDown
    if error == "expired_token":
        raise AuthError("Der Code ist abgelaufen, bevor er bestätigt wurde.")
    if error == "access_denied":
        raise AuthError("Die Anmeldung wurde abgelehnt.")
    raise AuthError(
        f"{url} antwortete mit HTTP {status}: "
        f"{payload.get('error_description') or error or 'ohne Angabe'}"
    )
