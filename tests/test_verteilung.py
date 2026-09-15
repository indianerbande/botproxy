"""Was der ZIP-Download enthält: der Betrieb, und keine Skripte."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent

SKRIPT_ENDUNGEN = (".sh", ".ps1", ".bat", ".cmd")
AUSNAHMEN = {"start-botproxy.cmd.example"}

BETRIEB = {"README.md", "requirements.txt", "start-botproxy.cmd.example"}


def _git(*args: str, **env: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        env={**os.environ, **env},
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def ausgeliefert(tmp_path_factory) -> list[str]:
    """The files the ZIP download would contain, from the working tree.

    `git archive HEAD` only sees what is committed, and this test should catch
    a new file before it is. So the working tree goes into a throwaway index —
    tracked and new files, minus `.gitignore` — and `git archive` itself packs
    it. Rebuilding its `export-ignore` rules by hand got directory patterns
    wrong; asking git does not. The real index is not touched.
    """
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("kein Git-Arbeitsverzeichnis")
    index = str(tmp_path_factory.mktemp("index") / "index")
    _git("add", "--all", GIT_INDEX_FILE=index)
    baum = _git("write-tree", GIT_INDEX_FILE=index).decode().strip()
    archiv = _git("archive", "--format=tar", "--worktree-attributes", baum)
    with tarfile.open(fileobj=io.BytesIO(archiv)) as tar:
        return sorted(m.name for m in tar.getmembers() if m.isfile())


def test_nur_was_der_betrieb_braucht(ausgeliefert):
    """A new file lands on the target machine unless `.gitattributes` says no."""
    fremd = [
        d
        for d in ausgeliefert
        if d not in BETRIEB and not (d.startswith("botproxy/") and d.endswith(".py"))
    ]
    assert fremd == [], f"in .gitattributes als export-ignore eintragen: {fremd}"
    assert set(ausgeliefert) >= BETRIEB


def test_keine_skripte(ausgeliefert):
    skripte = []
    for datei in ausgeliefert:
        if datei in AUSNAHMEN:
            continue
        if datei.lower().endswith(SKRIPT_ENDUNGEN):
            skripte.append(datei)
            continue
        with open(REPO / datei, "rb") as f:
            if f.read(2) == b"#!":
                skripte.append(datei)
    assert skripte == []


def test_ausgeschlossene_skripte_liegen_wirklich_ausserhalb(ausgeliefert):
    """The guard above is only worth something if it would see the hook."""
    assert "hooks/pre-commit" not in ausgeliefert
    assert (REPO / "hooks" / "pre-commit").read_bytes().startswith(b"#!")
