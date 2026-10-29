from __future__ import annotations

import os
import socket
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


# ------------------------------------------------------------------ demo app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def demo_url():
    from proofloop.web.demo.server import DemoServer

    with DemoServer(_free_port()) as server:
        yield server.url


@pytest.fixture
def demo_study(settings, demo_url):
    from proofloop.core.config import AllowedHost, load_yaml_file
    from proofloop.web.study import Study

    port = int(demo_url.rsplit(":", 1)[1])
    settings.safety.allowlist.append(AllowedHost(host="127.0.0.1", ports=[port], sandbox=True))
    raw = load_yaml_file(REPO / "examples" / "studies" / "demo.yaml")
    raw["target"]["url"] = demo_url
    return Study.model_validate(raw)


@pytest.fixture
def profile(settings):
    from proofloop.web.calibration.profiles import load_profile

    return load_profile(settings)
