"""Notification targets in the window's API: adding, changing, testing, and keeping secrets off the page."""

import json
import smtplib

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core import notify
from proofloop.monitor.printer.state import PrinterState
from proofloop.monitor.service import App


class FakeLink:
    def __init__(self, p):
        self.state = PrinterState(online=True)

    def add_listener(self, fn):
        pass

    def connect(self):
        pass


MAIL = {"kind": "email", "smtp_host": "mail.example.com", "smtp_user": "me@example.com", "smtp_password": "hunter2",
        "mail_to": "a@example.com, b@example.com", "min_level": "warning"}
BOT = {"kind": "telegram", "bot_token": "123:SECRETBOT", "chat_id": "42"}


@pytest.fixture
def farm(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    return TestClient(create_app(app)), TestClient(create_app(app, allow_send=False)), app


def saved(tmp_path):
    return json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["notifications"]


def test_secrets_are_saved_but_never_come_back(farm, tmp_path):
    c, _, app = farm
    assert c.post("/api/notifications", json=MAIL).json() == {"count": 1, "index": 0}
    assert c.post("/api/notifications", json=BOT).json()["index"] == 1
    assert saved(tmp_path)[0]["smtp_password"] == "hunter2" and saved(tmp_path)[1]["bot_token"] == "123:SECRETBOT"
    state = c.get("/api/state").json()
    for secret in ("hunter2", "SECRETBOT"):
        assert secret not in json.dumps(state)
    mail, bot = state["notifications"]
    assert mail["has_smtp_password"] and mail["mail_to"] == ["a@example.com", "b@example.com"] and mail["mail_from"] == "me@example.com"
    assert bot["has_bot_token"] and bot["chat_id"] == "42"
    assert app.settings.notifications[0].smtp_password == "hunter2"


def test_an_empty_secret_keeps_the_saved_one_and_a_new_one_replaces_it(farm, tmp_path):
    c, _, app = farm
    c.post("/api/notifications", json=MAIL)
    r = c.put("/api/notifications/0", json={**MAIL, "smtp_password": "", "smtp_host": "smtp.other.example", "min_level": "info"})
    assert r.status_code == 200
    n = app.settings.notifications[0]
    assert (n.smtp_password, n.smtp_host, n.min_level) == ("hunter2", "smtp.other.example", "info")
    c.put("/api/notifications/0", json={**MAIL, "smtp_password": "new-one"})
    assert saved(tmp_path)[0]["smtp_password"] == "new-one"
    c.put("/api/notifications/0", json={**BOT})  # another kind does not inherit a password
    assert saved(tmp_path)[0]["smtp_password"] is None


def test_replacing_the_list_keeps_each_targets_secrets_by_position_or_keep(farm):
    c, _, app = farm
    c.post("/api/notifications", json=MAIL)
    c.post("/api/notifications", json=BOT)
    both = [{**MAIL, "smtp_password": ""}, {**BOT, "bot_token": ""}]
    assert c.put("/api/notifications", json=both).json() == {"count": 2}
    assert [n.smtp_password for n in app.settings.notifications] == ["hunter2", None]
    assert app.settings.notifications[1].bot_token == "123:SECRETBOT"
    c.put("/api/notifications", json=[{**BOT, "bot_token": "", "keep": 1}])  # the first one removed
    assert [n.bot_token for n in app.settings.notifications] == ["123:SECRETBOT"]


def test_removing_a_target(farm):
    c, _, app = farm
    c.post("/api/notifications", json=MAIL)
    c.post("/api/notifications", json=BOT)
    assert c.delete("/api/notifications/0").json() == {"count": 1}
    assert [n.kind for n in app.settings.notifications] == ["telegram"]
    assert c.delete("/api/notifications/5").status_code == 404
    assert c.post("/api/notifications/5/test").status_code == 404


def test_mistakes_are_explained_in_plain_words(farm):
    c, _, app = farm
    r = c.post("/api/notifications", json={**MAIL, "mail_to": ""})
    assert r.status_code == 422 and r.json()["detail"] == "enter at least one address to send to"
    assert c.post("/api/notifications", json={**BOT, "bot_token": "x"}).status_code == 422
    r = c.post("/api/notifications", json={"kind": "ntfy", "url": "ntfy.sh/topic"})
    assert r.status_code == 422 and "http" in r.json()["detail"]
    assert c.post("/api/notifications", json={"kind": "fax"}).status_code == 422
    assert app.settings.notifications == []


def test_a_test_goes_to_that_target_whatever_its_level(farm, monkeypatch):
    c, _, _ = farm
    sent = []

    class Smtp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            if password != "hunter2":
                raise smtplib.SMTPAuthenticationError(535, b"no")

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(notify.smtplib, "SMTP", Smtp)
    c.post("/api/notifications", json={**MAIL, "min_level": "error"})
    r = c.post("/api/notifications/0/test").json()
    assert r["ok"] and len(sent) == 1 and sent[0]["Subject"] == "ProofLoop: test"
    c.put("/api/notifications/0", json={**MAIL, "smtp_password": "wrong"})
    r = c.post("/api/notifications/0/test").json()
    assert not r["ok"] and "username or password" in r["detail"] and "wrong" not in r["detail"]


def test_a_phone_sees_less_and_changes_nothing(farm, tmp_path):
    c, phone, app = farm
    c.post("/api/notifications", json=MAIL)
    c.post("/api/notifications", json={"kind": "discord", "url": "https://discord.com/api/webhooks/1/WEBHOOKPATH"})
    seen = phone.get("/api/state").json()["notifications"]
    assert [n["kind"] for n in seen] == ["email", "discord"]
    text = json.dumps(seen)
    for hidden in ("hunter2", "WEBHOOKPATH", "a@example.com", "me@example.com"):
        assert hidden not in text
    assert phone.post("/api/notifications", json=BOT).status_code == 403
    assert phone.put("/api/notifications/0", json=BOT).status_code == 403
    assert phone.put("/api/notifications", json=[]).status_code == 403
    assert phone.delete("/api/notifications/0").status_code == 403
    assert phone.post("/api/notifications/0/test").status_code == 403
    assert len(app.settings.notifications) == 2
