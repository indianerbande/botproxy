"""Erneuern, anmelden, und was dabei genau einmal passieren muss."""

from __future__ import annotations

import contextlib
import threading

from botproxy import config, store, tokens
from tests.stubs import make_jwt


def test_ablauf_wird_aus_dem_token_gelesen(signed_in):
    left = signed_in.remaining()
    assert left is not None
    assert 3500 < left.total_seconds() <= 3600


def test_gueltiges_token_loest_keine_erneuerung_aus(signed_in, idp):
    for _ in range(5):
        signed_in.ensure_fresh()
    assert idp.refreshes == 0


def test_ablaufendes_token_wird_erneuert(expiring, idp):
    alt = expiring.ensure_fresh()
    # Inside the margin, but not yet expired: renewal happens on the watchdog's
    # schedule, not on the request path.
    assert idp.refreshes == 0

    expiring.force_refresh()
    assert idp.refreshes == 1
    assert expiring.ensure_fresh() != alt


def test_rotiertes_refresh_token_ersetzt_das_alte(expiring, idp):
    idp.rotate = True
    expiring.force_refresh()

    record = store.read_refresh(config.REFRESH_FILE)
    assert record is not None
    # Keeping the old one would not fail now — it would fail at the *next*
    # renewal, and then nobody looks here.
    assert record.token != "refresh-alt"
    assert record.belongs_to(expiring.ensure_fresh())


def test_fremdes_refresh_token_wird_nicht_benutzt(settings, idp, tmp_path):
    """A hand-placed access token must not be undone by a stale pairing."""
    store.write_secret(config.TOKEN_FILE, make_jwt(3600))
    store.write_refresh(config.REFRESH_FILE, "refresh-fremd", make_jwt(3600))

    manager = tokens.Manager()
    try:
        manager.force_refresh()
    except tokens.LoginRequired:
        pass
    finally:
        manager.stop()

    assert idp.refreshes == 0
    assert idp.device_requests == 1


def test_zehn_gleichzeitige_anfragen_loesen_eine_erneuerung_aus(expiring, idp):
    """The whole reason the lock exists — `yacli` never had to answer this."""
    fehler: list[Exception] = []
    start = threading.Barrier(10)

    def anfrage():
        try:
            # Everyone holds the same token, and everyone was refused with it.
            stale = expiring.ensure_fresh()
            start.wait(timeout=5)
            expiring.force_refresh(stale=stale)
        except Exception as exc:  # noqa: BLE001
            fehler.append(exc)

    threads = [threading.Thread(target=anfrage) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not fehler
    # Ten callers, one exchange at the provider.
    assert idp.refreshes == 1


def test_totes_refresh_token_startet_die_anmeldung(expiring, idp):
    idp.refresh_dead = True

    try:
        expiring.force_refresh()
    except tokens.LoginRequired as exc:
        # Link and code must be in the message: that is what the client shows
        # in its own interface, where the user is actually looking.
        assert "ABCD-EFGH" in str(exc)
        assert exc.pending.verification_uri.startswith("http://")
    else:
        raise AssertionError("Anmeldung wurde nicht ausgelöst")

    assert idp.device_requests == 1


def test_anmeldung_laeuft_nur_einmal_trotz_vieler_anfragen(expiring, idp):
    idp.refresh_dead = True
    idp.pending_polls = 3

    for _ in range(8):
        with contextlib.suppress(tokens.LoginRequired):
            expiring.force_refresh()

    # One device code for everyone; the rest get the same link back.
    assert idp.device_requests == 1


def test_anmeldung_endet_mit_gueltigem_token(settings, idp):
    """The full round: no token, device code, confirmation, usable token."""
    idp.pending_polls = 1

    manager = tokens.Manager()
    try:
        with contextlib.suppress(tokens.LoginRequired):
            manager.ensure_fresh()

        for _ in range(60):
            if manager.snapshot()["zustand"] == "ok":
                break
            threading.Event().wait(0.2)

        assert manager.snapshot()["zustand"] == "ok"
        token = manager.ensure_fresh()
        assert store.jwt_expiry(token) is not None
        # Both halves on disk, and paired with each other.
        record = store.read_refresh(config.REFRESH_FILE)
        assert record is not None
        assert record.belongs_to(token)
    finally:
        manager.stop()
