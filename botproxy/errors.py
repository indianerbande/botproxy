"""Was schiefgehen kann, nach Zuständigkeit getrennt.

The distinction matters for the answer sent back to the client: a ConfigError
is the operator's problem and stops the process, an AuthError may resolve
itself on the next attempt, and a UpstreamError is not ours at all.
"""

from __future__ import annotations


class BotproxyError(Exception):
    """Base class, so callers can catch everything of ours and nothing else."""


class ConfigError(BotproxyError):
    """A value is missing, a placeholder, or unusable. Fatal at startup."""


class AuthError(BotproxyError):
    """The identity provider refused or could not be reached."""


class UpstreamError(BotproxyError):
    """The endpoint did not answer."""
