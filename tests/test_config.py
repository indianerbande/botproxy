"""Die Vorlage startet nicht — und jeder Platzhalter wird beim Namen genannt."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from botproxy import __main__, _env, config
from botproxy.errors import ConfigError

VORLAGE = Path(__file__).parent.parent / "start-botproxy.cmd.example"

# A configuration that passes, to change one value at a time against.
GUELTIG = {
    "BOTPROXY_BASE_URL": "http://127.0.0.1:1/v1",
    "BOTPROXY_AUTHORITY": "http://127.0.0.1:2",
    "BOTPROXY_CLIENT_ID": "test-client",
}


def vorlage() -> dict[str, str]:
    """The `set` lines of the Windows template, as the environment would see them."""
    werte: dict[str, str] = {}
    for zeile in VORLAGE.read_text(encoding="utf-8").splitlines():
        if zeile.startswith("set BOTPROXY_"):
            name, _, wert = zeile.removeprefix("set ").partition("=")
            werte[name] = wert
    return werte


PLATZHALTER = sorted(
    (name, wert)
    for name, wert in vorlage().items()
    if _env.looks_like_placeholder(wert)
)


@pytest.fixture
def umgebung(tmp_path):
    """Load `config` the way a start does: from the environment, at import.

    Setting attributes directly, as the other tests do, would skip the very
    step under test — reading the variables. So the module is reloaded, and
    reloaded once more afterwards from a clean environment.
    """
    mp = pytest.MonkeyPatch()
    for name in list(os.environ):
        if name.startswith("BOTPROXY_"):
            mp.delenv(name)

    def laden(werte: dict[str, str]) -> None:
        for name, wert in {**werte, "BOTPROXY_HOME": str(tmp_path)}.items():
            mp.setenv(name, wert)
        importlib.reload(config)

    yield laden
    mp.undo()
    importlib.reload(config)


def test_vorlage_enthaelt_platzhalter():
    """Guards the tests below: without placeholders they would test nothing."""
    namen = {name for name, _ in PLATZHALTER}
    assert {"BOTPROXY_BASE_URL", "BOTPROXY_AUTHORITY", "BOTPROXY_CLIENT_ID"} <= namen


def test_unveraenderte_vorlage_startet_nicht(
    umgebung, idp, tmp_path, capsys, monkeypatch
):
    """Copied, double-clicked, nothing filled in: a message, exit code 1, and
    nothing else — no sign-in, no browser, no key written, no port."""
    umgebung(vorlage())
    monkeypatch.setattr("sys.argv", ["botproxy"])

    assert __main__.main() == 1

    meldung = capsys.readouterr().err
    assert "BOTPROXY_BASE_URL" in meldung
    assert "Platzhalter" in meldung
    assert idp.device_requests == 0
    assert not (tmp_path / "local_key").exists()


def test_gueltige_werte_bestehen_die_pruefung(umgebung):
    """The control: what the tests below break is only the one placeholder."""
    umgebung(GUELTIG)
    config.validate()


@pytest.mark.parametrize(("name", "wert"), PLATZHALTER)
def test_jeder_platzhalter_wird_beim_namen_genannt(umgebung, name, wert):
    umgebung({**GUELTIG, name: wert})

    with pytest.raises(ConfigError) as fehler:
        config.validate()

    # The variable name is the one thing the reader can act on.
    assert name in str(fehler.value)
    assert "Platzhalter" in str(fehler.value)


@pytest.mark.parametrize(
    ("name", "wert"),
    [
        ("BOTPROXY_CLIENT_ID", "00000000-0000-0000-0000-000000000000"),
        ("BOTPROXY_BASE_URL", "https://api.example.invalid/v1"),
        ("BOTPROXY_AUTHORITY", "https://idp.example.invalid"),
    ],
)
def test_andere_platzhalter_werden_ebenso_erkannt(umgebung, name, wert):
    umgebung({**GUELTIG, name: wert})

    with pytest.raises(ConfigError, match=name):
        config.validate()


@pytest.mark.parametrize("name", sorted(GUELTIG))
def test_fehlender_wert_wird_beim_namen_genannt(umgebung, name):
    werte = {k: v for k, v in GUELTIG.items() if k != name}
    umgebung(werte)

    with pytest.raises(ConfigError, match=f"{name} ist nicht gesetzt"):
        config.validate()


def test_schraegstrich_am_ende_faellt_weg(umgebung):
    """Paths are appended with a leading slash; a trailing one would double it."""
    from botproxy import forward, tokens

    umgebung(
        {
            **GUELTIG,
            "BOTPROXY_BASE_URL": "http://127.0.0.1:1/v1/",
            "BOTPROXY_AUTHORITY": "http://127.0.0.1:2/",
        }
    )
    config.validate()

    f = forward.Forwarder(tokens.Manager())
    try:
        ziel = f.target("/v1/chat/completions", "")
    finally:
        f.close()
    assert ziel == "http://127.0.0.1:1/v1/chat/completions"
    assert config.AUTHORITY == "http://127.0.0.1:2"
