"""Gemeinsame Vorbereitung: Stubs, ein leeres Zuhause, gekappte Seiteneffekte."""

from __future__ import annotations

import pytest

from botproxy import config, tokens
from tests.stubs import Idp, Upstream, make_jwt


@pytest.fixture
def upstream():
    stub = Upstream()
    yield stub
    stub.close()


@pytest.fixture
def idp():
    stub = Idp()
    yield stub
    stub.close()


@pytest.fixture(autouse=True)
def settings(tmp_path, upstream, idp, monkeypatch):
    """Point every module at the stubs and at a throwaway directory.

    `config` reads the environment once at import, so tests set the attributes
    directly — the modules look them up when they run, not when they load.
    """
    monkeypatch.setattr(config, "BASE_URL", f"{upstream.base}/v1")
    monkeypatch.setattr(config, "AUTHORITY", idp.base)
    monkeypatch.setattr(config, "CLIENT_ID", "test-client")
    monkeypatch.setattr(config, "SCOPES", "openid offline_access")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(config, "REFRESH_FILE", tmp_path / "refresh.json")
    monkeypatch.setattr(config, "LOCAL_KEY_FILE", tmp_path / "local_key")
    monkeypatch.setattr(config, "REFRESH_MARGIN_SECONDS", 300)
    monkeypatch.setattr(config, "CHECK_INTERVAL_SECONDS", 1)
    # Nothing in a test run may open a browser window.
    monkeypatch.setattr(tokens.webbrowser, "open", lambda _url: True)
    return tmp_path


@pytest.fixture
def signed_in(settings, idp):
    """A manager with a valid token and a matching refresh record."""
    from botproxy import store

    access = make_jwt(3600)
    store.write_secret(config.TOKEN_FILE, access)
    store.write_refresh(config.REFRESH_FILE, "refresh-fest", access)
    idp.rotate = False
    manager = tokens.Manager()
    yield manager
    manager.stop()


@pytest.fixture
def expiring(settings, idp):
    """A manager whose token is inside the renewal margin."""
    from botproxy import store

    access = make_jwt(30)
    store.write_secret(config.TOKEN_FILE, access)
    store.write_refresh(config.REFRESH_FILE, "refresh-alt", access)
    manager = tokens.Manager()
    yield manager
    manager.stop()
