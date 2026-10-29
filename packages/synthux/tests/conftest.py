from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Never touch the real ClearLoop folder: loop files made in tests are kept in a temporary one.
os.environ["CLEARLOOP_DATA"] = tempfile.mkdtemp(prefix="clearloop-test-")

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


# ------------------------------------------------------------------ demo app

import socket
import sys

sys.path.insert(0, str(REPO))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def demo_url():
    from demo_app.server import DemoServer

    with DemoServer(_free_port()) as server:
        yield server.url


@pytest.fixture
def demo_study(settings, demo_url):
    from synthux.core.config import AllowedHost, load_yaml_file
    from synthux.core.study import Study

    port = int(demo_url.rsplit(":", 1)[1])
    settings.safety.allowlist.append(AllowedHost(host="127.0.0.1", ports=[port], sandbox=True))
    raw = load_yaml_file(REPO / "studies" / "demo.yaml")
    raw["target"]["url"] = demo_url
    return Study.model_validate(raw)


@pytest.fixture
def profile(settings):
    from synthux.calibration.profiles import load_profile

    return load_profile(settings)
