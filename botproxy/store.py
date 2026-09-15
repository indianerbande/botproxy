"""Geheimnisse auf die Platte und wieder zurück.

Two things here are not obvious, and both bite exactly once: writing
through a temporary file, and pairing the refresh token with the access token
it was issued with.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from botproxy.errors import ConfigError

REFRESH_FORMAT_VERSION = 1


def mask(token: str) -> str:
    """Enough to recognise a token, not enough to use one."""
    if len(token) <= 12:
        return "…"
    return f"{token[:4]}…{token[-4:]}"


def fingerprint(token: str) -> str:
    """A short, one-way mark of a token — enough to recognise it, useless alone."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def jwt_expiry(token: str) -> datetime | None:
    """`exp` from the payload, or None if this is not a JWT.

    The signature is deliberately not checked — that is the endpoint's job, and
    we have no key. Only `exp` is read; the rest of the payload carries
    internal claims that must never be logged or displayed.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    raw = parts[1]
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int | float) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(exp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def write_secret(path: Path, value: str) -> Path:
    """Write to a temporary file first, then rename.

    A crash mid-write would otherwise leave a truncated secret, and the next
    request would fail in a way that looks like a server problem.

    Who may read the file is decided by the ACL it inherits from the user
    profile, not by mode bits — `chmod` on Windows only toggles read-only.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(value.strip() + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise ConfigError(f"Konnte {path} nicht schreiben: {exc}") from exc
    return path


@dataclass(frozen=True)
class RefreshRecord:
    """A refresh token plus the access token it was issued with.

    A refresh token always belongs to one particular sign-in. Using it against
    an access token that came from somewhere else — hand-placed, say — would
    quietly replace what someone just put there.
    """

    token: str
    access_fingerprint: str | None

    def belongs_to(self, access_token: str) -> bool:
        if self.access_fingerprint is None:
            return False
        return hmac.compare_digest(self.access_fingerprint, fingerprint(access_token))


def read_token(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Token-Datei {path} nicht lesbar: {exc}") from exc
    return content or None


def read_refresh(path: Path) -> RefreshRecord | None:
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        # Not fatal: without it renewal is impossible, but signing in still works.
        return None
    if not content:
        return None
    try:
        payload = json.loads(content)
    except ValueError:
        # A bare token, placed by hand or by an older version: we cannot tell
        # which sign-in it belongs to, so it counts as orphaned.
        return RefreshRecord(token=content, access_fingerprint=None)
    if not isinstance(payload, dict) or not payload.get("refresh_token"):
        raise ConfigError(f"{path} ist unlesbar. Lösche die Datei.")
    return RefreshRecord(
        token=str(payload["refresh_token"]),
        access_fingerprint=payload.get("access_fingerprint"),
    )


def write_refresh(path: Path, token: str | None, access_token: str | None) -> None:
    """Keep or drop the refresh token, together with the pairing it belongs to.

    Passing None removes the file — a provider that stops issuing one must not
    leave a stale token behind that later fails in a confusing way.
    """
    if token is None:
        path.unlink(missing_ok=True)
        return
    payload = {
        "version": REFRESH_FORMAT_VERSION,
        "refresh_token": token,
        "access_fingerprint": fingerprint(access_token) if access_token else None,
    }
    write_secret(path, json.dumps(payload))
