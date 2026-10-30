"""Testing a printer's connection the way a send uses it, against a pretend printer."""

from proofloop.monitor.connection import _ftps, check_connection
from proofloop.monitor.printer.pretend import FakePrinter


def run(p: FakePrinter, **kw):
    args = {"host": "127.0.0.1", "serial": p.serial, "access_code": p.access_code, "mqtt_port": p.mqtt_port,
            "ftp_port": p.ftp_port, "timeout": 3.0} | kw
    return check_connection(**args)


def test_a_printer_that_works():
    p = FakePrinter()
    try:
        r = run(p)
        assert r["ok"] and [s["step"] for s in r["steps"]] == ["mqtt", "reports", "ftps"]
        assert "it is idle" in r["steps"][1]["detail"] and not p.files  # nothing was copied
    finally:
        p.close()


def test_each_problem_says_what_to_fix():
    p = FakePrinter()
    try:
        r = run(p, access_code="wrong")
        assert not r["ok"] and len(r["steps"]) == 1 and "check the access code" in r["steps"][0]["detail"]
        r = run(p, serial="01P00A999999999")
        assert r["steps"][1] == {"step": "reports", "ok": False, "detail": r["steps"][1]["detail"]}
        assert "check the serial number" in r["steps"][1]["detail"]
        assert not _ftps("127.0.0.1", "wrong", p.ftp_port, 3.0)["ok"]
        p.state["hms"] = [{"attr": 0x05000500, "code": 0x00010007}]  # MQTT command verification failed
        r = run(p)
        assert "turn on Developer Mode" in r["steps"][1]["detail"] and not r["ok"]
    finally:
        p.close()
    r = check_connection("127.0.0.1", "S", "x", mqtt_port=p.mqtt_port, timeout=2.0)  # nothing listens any more
    assert not r["ok"] and "Can't reach" in r["steps"][0]["detail"]
