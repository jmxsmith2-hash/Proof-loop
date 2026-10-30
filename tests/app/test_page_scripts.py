"""The Loop files & Printers page's script files: they share one global scope, so no name may be declared twice."""

import re
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor.service import App

JS = Path(__file__).resolve().parents[2] / "src" / "proofloop" / "app" / "static" / "js"


def top_level_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        m = re.match(r"(?:async\s+)?function\s+([\w$]+)", line) or re.match(r"class\s+([\w$]+)", line)
        if m:
            names.append(m.group(1))
            continue
        m = re.match(r"(?:const|let|var)\s+(.+)", line)
        if m:
            decl = m.group(1)
            while re.search(r"\([^()]*\)", decl):  # leave out function parameters and calls
                decl = re.sub(r"\([^()]*\)", "", decl)
            names += re.findall(r"(?:^|,\s*)([A-Za-z_$][\w$]*)\s*(?==|,|;|$)", decl)
    return names


def test_no_name_is_declared_twice_across_the_files():
    names = Counter(n for f in sorted(JS.glob("*.js")) for n in top_level_names(f.read_text(encoding="utf-8")))
    assert names["STATE"] == 1 and names["refreshOrders"] == 1  # the parser sees them
    assert [n for n, c in names.items() if c > 1] == []


def test_the_page_loads_every_file_and_each_is_served(tmp_path):
    c = TestClient(create_app(App(tmp_path, connect=False)))
    page = c.get("/").text
    files = re.findall(r'<script src="/js/([\w-]+)\.js"></script>', page)
    assert sorted(files) == sorted(f.stem for f in JS.glob("*.js")) and files[0] == "core" and files[-1] == "main"
    for name in files:
        r = c.get(f"/js/{name}.js")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert c.get("/js/..%2Fshell.js").status_code == 404 and c.get("/js/nothing.js").status_code == 404


def test_live_updates_say_what_changed(tmp_path):
    import json
    import threading

    app = App(tmp_path, connect=False)
    c = TestClient(create_app(app))
    threading.Timer(0.3, lambda: app.store.add_event(None, "info", "test", "something happened")).start()
    with c.stream("GET", "/api/live?limit=2") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        data = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert set(data[0]) == {"printers", "events", "queue", "orders", "filament"}  # the first time: everything
    assert data[1] == ["events"]
