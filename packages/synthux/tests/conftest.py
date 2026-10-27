from __future__ import annotations

from pathlib import Path

import pytest

from synthux.core.config import Settings, load_settings
from synthux.core.db import Database

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = load_settings(REPO / "config")
    s.data_dir = tmp_path / "data"
    return s


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite")
    yield database
    database.close()
