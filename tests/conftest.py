from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Never touch your real ~/.proofloop from tests.
os.environ["PROOFLOOP_HOME"] = tempfile.mkdtemp(prefix="proofloop-test-")

import pytest

from proofloop.core.config import Settings, load_settings
from proofloop.core.db import Database

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _own_proofloop_folder(tmp_path_factory, monkeypatch):
    """Every test gets an empty ProofLoop folder of its own (settings, printers, feedback)."""
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = load_settings(REPO / "src" / "proofloop" / "data")
    s.data_dir = tmp_path / "data"
    return s


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite")
    yield database
    database.close()
