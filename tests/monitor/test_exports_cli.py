"""proofloop printers export orders|history|costs."""

import csv
import json

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor.cli import main
from tests.monitor.test_orders import take
from tests.monitor.test_queue import farm


def with_an_order(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    app = farm(tmp_path, "a")
    take(TestClient(create_app(app)), price=2.0, customer="=Sam")
    app.store.add_outcome({"printer": "a", "job": "x", "started": 1.0, "ended": 3600.0, "outcome": "finished"})
    app.shutdown()


def test_export_orders_to_a_file_for_excel(tmp_path, monkeypatch, capsys):
    with_an_order(tmp_path, monkeypatch)
    out = tmp_path / "orders.csv"
    assert main(["export", "orders", "--out", str(out)]) == 0
    assert "Wrote 1 row" in capsys.readouterr().out
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    row = next(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    assert row["customer"] == "'=Sam" and row["parts"] == "40"


def test_export_prints_csv_or_json(tmp_path, monkeypatch, capsys):
    with_an_order(tmp_path, monkeypatch)
    assert main(["export", "history"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("printer,file,loop,started,ended,result") and "finished" in text and "\r" not in text
    assert main(["export", "orders", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["customer"] == "=Sam" and rows[0]["value"] == 80.0  # JSON is the data as it is: no quoting
    assert main(["export", "costs", "--by", "day", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["print_hours"] == 1.0


def test_export_takes_a_date_range_and_refuses_bad_ones(tmp_path, monkeypatch, capsys):
    with_an_order(tmp_path, monkeypatch)
    assert main(["export", "orders", "--json", "--from", "2001-01-01", "--to", "2001-12-31"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["export", "orders", "--from", "last week"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err
