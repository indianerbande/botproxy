"""Woher das Token kommt und wie es frisch bleibt.

Several requests arrive at once and nobody is watching the window. That is
what the lock and the watchdog below are for.

This module never writes to the screen. It reports through the `notify`
callback and through exceptions; `server.py` decides what becomes visible.
"""

from __future__ import annotations

import contextlib
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from botproxy import config, oauth, store
from botproxy.errors import AuthError

# A token younger than this that the endpoint refuses is not stale — it is
# refused for a reason another token will share: an issuer the endpoint does
# not accept, a missing permission. Renewing again would cost one round trip
# to the provider per request and change nothing.
FRESHLY_ISSUED_SECONDS = 60


@dataclass(frozen=True)
class PendingLogin:
    """A sign-in waiting for the human. Rendered into the error the client shows."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None

    @property
    def link(self) -> str:
        return self.verification_uri_complete or self.verification_uri


class LoginRequired(AuthError):
    """No usable token, and a sign-in is under way.

    Carries the pending login so the caller can put link and code where the
    user is actually looking — which is the IDE, not a terminal window behind
    it.
    """

    def __init__(self, pending: PendingLogin) -> None:
        super().__init__(
            f"Anmeldung nötig. Öffne {pending.link} und bestätige den Code "
            f"{pending.user_code}."
        )
        self.pending = pending


class Manager:
    """Holds the access token, renews it, and signs in when that fails.

    All state changes go through `_lock`. It has two occasions — renewal and
    sign-in — and in both it makes sure that ten simultaneous requests cause
    one operation, not ten.
    """

    def __init__(self, notify: Callable[[str], None] | None = None) -> None:
        self._lock = threading.RLock()
        self._notify = notify or (lambda _message: None)
        self._access: str | None = None
        self._expires_at: datetime | None = None
        self._issued_at: datetime | None = None
        self._refusal_reported = False
        self._pending: PendingLogin | None = None
        # Set whenever no sign-in is running. Cleared when one starts, set again
        # when it ends — however it ends. `wait_for_login` sleeps on it.
        self._login_over = threading.Event()
        self._login_over.set()
        # A sign-in ran out without anyone confirming. Nobody is at the screen,
        # so the watchdog leaves it at that; the next request starts a new one.
        self._login_unanswered = False
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._load_from_disk()

    # --- Zustand -------------------------------------------------------------

    def _load_from_disk(self) -> None:
        token = store.read_token(config.TOKEN_FILE)
        if token:
            self._access = token
            self._expires_at = store.jwt_expiry(token)

    def remaining(self) -> timedelta | None:
        if self._expires_at is None:
            return None
        return self._expires_at - datetime.now(UTC)

    def _still_good(self, margin: int = 0) -> bool:
        """Usable for the next `margin` seconds.

        A token without a readable `exp` counts as good: it may be a static key
        against a local endpoint, and refusing it would break that case for no
        gain. It simply never triggers renewal.
        """
        if self._access is None:
            return False
        left = self.remaining()
        if left is None:
            return True
        return left.total_seconds() > margin

    def snapshot(self) -> dict[str, object]:
        """What `status` reports. No token in here, not even masked."""
        with self._lock:
            left = self.remaining()
            if self._pending is not None:
                state = "anmeldung_läuft"
            elif self._still_good():
                state = "ok"
            else:
                state = "kein_token"
            return {
                "zustand": state,
                "gueltig_bis": (
                    self._expires_at.astimezone().isoformat()
                    if self._expires_at
                    else None
                ),
                "restlaufzeit_sekunden": (
                    int(left.total_seconds()) if left is not None else None
                ),
            }

    # --- Nach außen ----------------------------------------------------------

    def ensure_fresh(self) -> str:
        """The token for the next request.

        Normally this returns immediately — the watchdog has done the work
        before a request ever arrives. It only does anything when the watchdog
        was too slow or has not run yet.
        """
        with self._lock:
            if self._pending is not None:
                raise LoginRequired(self._pending)
            if self._still_good():
                return self._access  # type: ignore[return-value]
            # A request means someone is at the client — worth asking again.
            self._login_unanswered = False
            self._renew_or_login()
            if self._pending is not None:
                raise LoginRequired(self._pending)
            if self._access is None:
                raise AuthError("Kein Zugangstoken verfügbar.")
            return self._access

    def force_refresh(self, stale: str | None = None) -> str:
        """Renew even though the token still looks valid.

        The endpoint answered 401 — its opinion beats our arithmetic. Clock
        skew, a revoked session, a restarted issuer: all of them look fine from
        `exp` alone.

        `stale` is the token the caller was refused with. If it has already
        been replaced by then, someone else did the work while this caller was
        waiting for the lock, and renewing again would ask the provider ten
        times for what ten requests needed once.

        A token issued moments ago is not renewed either. The endpoint refusing
        it says the configuration is wrong, not the token — and the refusal
        goes through to the client, which shows the endpoint's own reason.
        """
        with self._lock:
            if stale is not None and self._access is not None and self._access != stale:
                return self._access
            if self._access is not None and self._freshly_issued():
                if not self._refusal_reported:
                    self._refusal_reported = True
                    self._notify(
                        "Der Endpunkt lehnt ein eben ausgestelltes Token ab. "
                        "Erneuern hilft da nicht — vermutlich gehören "
                        "BOTPROXY_AUTHORITY und BOTPROXY_BASE_URL nicht zur "
                        "selben Umgebung, oder die Berechtigung fehlt."
                    )
                return self._access
            self._expires_at = datetime.now(UTC)
            return self.ensure_fresh()

    def _freshly_issued(self) -> bool:
        if self._issued_at is None:
            return False
        age = datetime.now(UTC) - self._issued_at
        return age.total_seconds() < FRESHLY_ISSUED_SECONDS

    # --- Erneuern und Anmelden -----------------------------------------------

    def _renew_or_login(self) -> None:
        """Try the refresh token; if it is dead, sign in. Caller holds the lock."""
        record = store.read_refresh(config.REFRESH_FILE)
        if record is None:
            self._begin_login()
            return
        if self._access is not None and not record.belongs_to(self._access):
            # The token file was replaced from outside — by hand, most likely.
            # Renewing now would undo exactly what someone just did.
            self._notify(
                "Das gespeicherte Refresh-Token gehört zu einer anderen "
                "Anmeldung und wird nicht benutzt."
            )
            self._begin_login()
            return
        try:
            tokens = oauth.refresh(
                config.AUTHORITY, config.CLIENT_ID, record.token, config.SCOPES
            )
        except AuthError as exc:
            self._notify(f"Erneuerung fehlgeschlagen: {exc}")
            self._begin_login()
            return
        self._adopt(tokens.access, tokens.refresh or record.token)
        self._notify(f"Token erneuert, {self.describe_validity()}")

    def _adopt(self, access: str, refresh_token: str | None) -> None:
        """Take a fresh pair into use and onto disk. Caller holds the lock."""
        store.write_secret(config.TOKEN_FILE, access)
        # The provider may hand out a new refresh token and retire the old one;
        # keeping the old one would fail at the *next* renewal, not this one.
        store.write_refresh(config.REFRESH_FILE, refresh_token, access)
        self._access = access
        self._expires_at = store.jwt_expiry(access)
        self._issued_at = datetime.now(UTC)
        self._refusal_reported = False
        self._login_unanswered = False
        self._pending = None

    def _begin_login(self) -> None:
        """Ask for a device code and let a thread wait for confirmation.

        Waiting here would block the request that triggered this, and the client
        would run into a timeout with nothing to show for it. So the code is
        published immediately and the polling happens elsewhere.
        """
        if self._pending is not None:
            return
        code = oauth.request_code(config.AUTHORITY, config.CLIENT_ID, config.SCOPES)
        self._pending = PendingLogin(
            user_code=code.user_code,
            verification_uri=code.verification_uri,
            verification_uri_complete=code.verification_uri_complete,
        )
        self._login_over.clear()
        self._notify(
            f"Anmeldung nötig — öffne {self._pending.link} "
            f"und bestätige den Code {code.user_code}"
        )
        # A missing browser is not a failure: link and code are in the window.
        with contextlib.suppress(Exception):
            webbrowser.open(self._pending.link)
        thread = threading.Thread(
            target=self._await_login, args=(code,), name="botproxy-login", daemon=True
        )
        thread.start()

    def _await_login(self, code: oauth.DeviceCode) -> None:
        """Poll, and however that ends, report the sign-in as over."""
        try:
            self._poll_login(code)
        finally:
            self._login_over.set()

    def _poll_login(self, code: oauth.DeviceCode) -> None:
        """Poll until the user confirms, the code expires, or we give up."""
        interval = code.interval
        deadline = datetime.now(UTC) + timedelta(seconds=code.expires_in)
        while datetime.now(UTC) < deadline:
            if self._stop.wait(interval):
                return
            try:
                tokens = oauth.poll(
                    config.AUTHORITY, config.CLIENT_ID, code.device_code
                )
            except oauth.SlowDown:
                interval += oauth.SLOW_DOWN_STEP
                continue
            except AuthError as exc:
                self._give_up(f"Anmeldung abgebrochen: {exc}")
                return
            if tokens is None:
                continue
            with self._lock:
                self._adopt(tokens.access, tokens.refresh)
            self._notify(f"Angemeldet, {self.describe_validity()}")
            return
        self._give_up("Der Anmeldecode ist abgelaufen, bevor er bestätigt wurde.")

    def _give_up(self, reason: str) -> None:
        """End a sign-in that nobody completed, and do not start the next one.

        Starting over right away would open a browser tab every quarter of an
        hour for as long as nobody is there — a whole night's worth by morning.
        """
        with self._lock:
            self._pending = None
            self._login_unanswered = True
            serving = self._watchdog is not None
        if serving:
            reason += " Die nächste Anfrage startet eine neue Anmeldung."
        self._notify(reason)

    def wait_for_login(self) -> bool:
        """Block until a running sign-in is over. True if it left a usable token.

        Returns at once when no sign-in is running. The wait is cut into short
        slices only so that Ctrl-C gets through on Windows, where an unbounded
        wait on a lock does not see it; the wake-up itself comes from the
        sign-in thread, not from looking.
        """
        while not self._login_over.wait(0.5):
            pass
        with self._lock:
            return self._still_good()

    # --- Wecker --------------------------------------------------------------

    def start_watchdog(self) -> None:
        """Renew before a request arrives, not because one did.

        The proxy knows `exp` locally, so there is no reason to wait for a
        failure. When the refresh token turns out to be dead, the sign-in
        starts here — which usually means it is over by the time anyone sits
        down at the IDE again.

        It starts one, not one after another. A code that ran out unconfirmed
        says nobody is there, and from then on only a request asks again.
        """
        if self._watchdog is not None:
            return
        self._watchdog = threading.Thread(
            target=self._tick, name="botproxy-watchdog", daemon=True
        )
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()

    def _tick(self) -> None:
        while not self._stop.wait(config.CHECK_INTERVAL_SECONDS):
            with self._lock:
                if self._pending is not None or self._login_unanswered:
                    continue
                if self._still_good(config.REFRESH_MARGIN_SECONDS):
                    continue
                try:
                    self._renew_or_login()
                except AuthError as exc:
                    # A dead network is not a dead proxy: the current token may
                    # still have minutes left, and the next tick tries again.
                    self._notify(f"Erneuerung verschoben: {exc}")

    # --- Auskunft ------------------------------------------------------------

    def describe_validity(self) -> str:
        """One line about the token's life, for the window and for `status`."""
        if self._expires_at is None:
            return "kein JWT — keine Ablaufzeit ermittelbar"
        left = self.remaining()
        assert left is not None
        stamp = self._expires_at.astimezone().strftime("%H:%M")
        seconds = abs(left).total_seconds()
        if seconds < 60:
            spanne = "weniger als eine Minute"
        else:
            minutes = int(seconds) // 60
            spanne = "1 Minute" if minutes == 1 else f"{minutes} Minuten"
        if left.total_seconds() <= 0:
            return f"abgelaufen seit {spanne}"
        return f"gültig bis {stamp} (noch {spanne})"
