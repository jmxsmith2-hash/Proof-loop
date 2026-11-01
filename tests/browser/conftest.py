"""Browser tests: the real window, driven by Playwright's Chromium against ``proofloop try``.

Skipped (not failed) when Playwright or its Chromium isn't installed::

    pip install playwright && python -m playwright install chromium
    pytest -m browser

With ``PROOFLOOP_BROWSER_REQUIRED=1`` (CI) a missing browser is an error instead of a skip.

One ``proofloop try`` runs for the whole session, in a process of its own with an empty
``PROOFLOOP_HOME``: three pretend printers on free ports, the pretend printers' example run, and the
window's server on a free port. A separate process, because ProofLoop reads ``PROOFLOOP_HOME`` all
the time and the other tests change it for every test. Each test gets a fresh browser context and
makes its own data (spools and orders are named with a random suffix), so the tests don't depend on
each other or on their order.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
REQUIRED = os.environ.get("PROOFLOOP_BROWSER_REQUIRED") == "1"
INSTALL = "pip install playwright && python -m playwright install chromium"


def pytest_collection_modifyitems(items):
    """Everything in this folder is a ``browser`` test (``pytest -m browser`` / ``-m 'not browser'``)."""
    for item in items:
        if HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.browser)


def _unavailable(why: str):
    (pytest.fail if REQUIRED else pytest.skip)(f"{why} ({INSTALL})")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, headers: dict | None = None, timeout: float = 5):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


@dataclass
class Farm:
    """A running ``proofloop try``."""

    port: int
    home: Path
    log: Path
    proc: subprocess.Popen

    @property
    def url(self) -> str:
        # 127.0.0.1, not localhost: the window's origin check is for http://127.0.0.1:<port>
        return f"http://127.0.0.1:{self.port}"

    def tail(self, n: int = 40) -> str:
        try:
            return "\n".join(self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    def state(self) -> dict:
        return _get(self.url + "/api/state")


@pytest.fixture(scope="session")
def farm(tmp_path_factory) -> Iterator[Farm]:
    pytest.importorskip("playwright.sync_api")
    home = tmp_path_factory.mktemp("proofloop-home")
    log = tmp_path_factory.mktemp("proofloop-log") / "try.log"
    port = _free_port()
    env = {**os.environ, "PROOFLOOP_HOME": str(home), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    env.pop("PROOFLOOP_PRETEND", None)  # `proofloop try` sets it itself, for its own folder
    with open(log, "wb") as out:
        proc = subprocess.Popen([sys.executable, "-m", "proofloop", "try", "--no-window", "--port", str(port)],
                                cwd=REPO, env=env, stdout=out, stderr=subprocess.STDOUT)
    farm = Farm(port, home, log, proc)
    try:
        end = time.time() + 90
        while True:
            if proc.poll() is not None:
                pytest.fail(f"proofloop try stopped with {proc.returncode}:\n{farm.tail()}")
            try:
                _get(farm.url + "/proofloop/ping")
                online = [m["printer"]["online"] for m in farm.state()["monitor"]]
                if len(online) == 3 and all(online):  # three pretend printers connected
                    break
            except OSError:
                pass
            if time.time() > end:
                pytest.fail(f"the pretend farm didn't come up in 90 s:\n{farm.tail()}")
            time.sleep(0.25)
        yield farm
    finally:
        try:  # the way the window's Quit does it
            req = urllib.request.Request(farm.url + "/proofloop/quit", data=b"{}", method="POST",
                                         headers={"X-ProofLoop": "1", "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001 - whatever is left is stopped below
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture(scope="session")
def chromium() -> Iterator:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except sync_api.Error as e:
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                _unavailable("Playwright's Chromium isn't installed")
            raise
        yield launched
        launched.close()


@pytest.fixture
def window(chromium, farm):
    """The ProofLoop window, opened on the pretend farm. Fails the test on a console error or an uncaught
    exception in any of its pages (the window, Check, Loop files & Printers)."""
    from tests.browser.window import Window

    context = chromium.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
    page = context.new_page()
    page.set_default_timeout(20_000)
    w = Window(page, farm.url)
    try:
        w.open()
        yield w
        w.assert_clean()
    finally:
        context.close()
