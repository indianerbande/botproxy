"""Reading settings out of the environment, and checking them.

Split off from `config.py` so that module can start with the values
themselves: target address and identity provider are what someone opens the
file for, and they belong at the top.

Placeholders are rejected here rather than at the first request. A proxy that
starts with `HOST` in its URL and fails an hour later has only moved the
diagnosis.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from botproxy.errors import ConfigError

# Which variables the environment actually supplied — used by `status` to say
# where a value came from.
from_env: set[str] = set()

# Substrings that give away a template someone copied but never filled in.
PLACEHOLDERS = ("HOST", "PFAD", "MODELLNAME", "CLIENT-ID", ".invalid")
NULL_GUID = "00000000-0000-0000-0000-000000000000"


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    from_env.add(name)
    return value


def looks_like_placeholder(value: str) -> bool:
    if value == NULL_GUID:
        return True
    return any(marker in value for marker in PLACEHOLDERS)


def string(name: str, default: str = "") -> str:
    value = _raw(name)
    return default if value is None else value


def integer(name: str, default: int) -> int:
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} ist keine ganze Zahl: {value!r}") from exc


def require_url(name: str, value: str) -> str:
    """A usable absolute URL, or a message naming the variable.

    Checked at startup for both the endpoint and the identity provider. The
    trailing slash is stripped so callers can join paths without guessing
    whether one is already there.
    """
    if not value:
        raise ConfigError(
            f"{name} ist nicht gesetzt. Ohne Zieladresse kann botproxy nichts "
            "weiterreichen."
        )
    if looks_like_placeholder(value):
        raise ConfigError(
            f"{name} enthält noch einen Platzhalter: {value!r}. "
            "Trage die echte Adresse ein."
        )
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{name} ist keine brauchbare URL: {value!r}")
    return value.rstrip("/")


def require_value(name: str, value: str) -> str:
    if not value:
        raise ConfigError(f"{name} ist nicht gesetzt.")
    if looks_like_placeholder(value):
        raise ConfigError(f"{name} enthält noch einen Platzhalter: {value!r}.")
    return value
