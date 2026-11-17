"""Tests for the dev-only, read-only C3 probe (``scripts/c3_probe.py``).

``scripts/`` is not a package; the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "c3_probe.py"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("c3_probe", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def _write_env(root: Path, text: str) -> None:
    (root / ".env").write_text(text, encoding="utf-8")


def test_reads_only_the_documented_keys(probe, tmp_path):
    _write_env(
        tmp_path,
        "DB_USER=dbuser\nDB_PASSWORD=dbsecret\nMAIL_LOGIN=other\n"
        "ILET_EMAIL=owner@example.com\nILET_PASSWORD=pw\nILET_REGION=EU\n",
    )
    assert probe.load_env() == ("owner@example.com", "pw", "eu")


@pytest.mark.parametrize(
    "text",
    [
        # Unrelated credentials must never be picked up as a fallback.
        "DB_USER=dbuser\nDB_PASSWORD=dbsecret\nREGION=eu\n",
        "ILET_EMAIL=owner@example.com\nILET_REGION=eu\n",
        "ILET_EMAIL=owner@example.com\nILET_PASSWORD=pw\n",
        "ILET_EMAIL=owner@example.com\nILET_PASSWORD=pw\nILET_REGION=mars\n",
    ],
)
def test_missing_or_invalid_keys_exit(probe, tmp_path, text):
    _write_env(tmp_path, text)
    with pytest.raises(SystemExit) as exc:
        probe.load_env()
    # The message names keys but never echoes a value.
    assert "dbsecret" not in str(exc.value)
    assert "pw" not in str(exc.value).replace("ILET_PASSWORD", "")


def test_probe_cannot_send_control_frames(probe):
    """The probe only sends the integration's own query frames."""
    from custom_components.iletcomfort.api import build_c3_query

    assert not hasattr(probe, "cmd_set")
    assert not hasattr(probe, "build_c3_frame")
    assert probe.build_c3_query is build_c3_query
