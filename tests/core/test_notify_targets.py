"""Email and Telegram notification targets (nothing leaves this computer: SMTP is faked, Telegram is a local server)."""

import json
import smtplib
import threading
from email import message_from_bytes, policy
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proofloop.core import notify
from proofloop.core.settings import Notifier

PHOTO = b"\xff\xd8not-really-a-jpeg"
EVENT = {"kind": "plate_not_clear", "level": "error", "message": "Parts left on the plate.\nCheck it.", "printer": "p2s"}


class FakeSMTP:
    """Stands in for smtplib.SMTP and SMTP_SSL: records what a real one would be told."""

    log: list = []
    refuse_with_picture = False
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.calls = [("connect", host, port, bool(context))]
        FakeSMTP.log.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append(("quit",))

    def starttls(self, context=None):
        self.calls.append(("starttls", context is not None))

    def login(self, user, password):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"no")
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        if FakeSMTP.refuse_with_picture and any(p.get_filename() for p in msg.iter_attachments()):
            raise smtplib.SMTPDataError(552, b"too big")
        self.calls.append(("send", msg))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.log, FakeSMTP.refuse_with_picture, FakeSMTP.fail_login = [], False, False
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", type("SSL", (FakeSMTP,), {}))
    return FakeSMTP


def mail(**kw):
    return Notifier(kind="email", smtp_host="mail.example.com", smtp_user="me@example.com", smtp_password="hunter2",
                    mail_to=["a@example.com", "b@example.com"], min_level="warning", **kw)


def sent(conn):
    return next(c[1] for c in conn.calls if c[0] == "send")


def test_email_has_the_title_as_subject_the_message_as_body_and_the_picture_as_plate_jpg(smtp):
    n = notify.Notifier([mail()], farm_name="Farm").send(EVENT, PHOTO, wait=True)
    assert n == 1
    (conn,) = smtp.log
    assert [c[0] for c in conn.calls] == ["connect", "starttls", "login", "send", "quit"]  # STARTTLS before the login
    assert conn.calls[0][1:3] == ("mail.example.com", 587)
    assert conn.calls[2][1:] == ("me@example.com", "hunter2")
    msg = message_from_bytes(sent(conn).as_bytes(), policy=policy.default)
    assert msg["Subject"] == "Farm: p2s plate not clear" and msg["From"] == "me@example.com"  # From falls back to the user
    assert msg["To"] == "a@example.com, b@example.com"
    assert msg.get_body(("plain",)).get_content().strip() == EVENT["message"]
    (pic,) = list(msg.iter_attachments())
    assert pic.get_filename() == "plate.jpg" and pic.get_content_type() == "image/jpeg" and pic.get_content() == PHOTO


def test_email_without_a_picture_has_no_attachment_and_ssl_uses_its_own_port(smtp):
    assert notify.Notifier([mail(smtp_security="ssl")]).send(EVENT, None, wait=True) == 1
    (conn,) = smtp.log
    assert conn.calls[0][1:3] == ("mail.example.com", 465) and "starttls" not in [c[0] for c in conn.calls]
    assert not list(sent(conn).iter_attachments())


def test_email_tries_again_without_the_picture_when_the_server_refuses_the_message(smtp):
    smtp.refuse_with_picture = True
    assert notify.Notifier([mail()]).send(EVENT, PHOTO, wait=True) == 1
    assert len(smtp.log) == 2 and not list(sent(smtp.log[1]).iter_attachments())


def test_email_respects_the_lowest_level(smtp):
    quiet = {**EVENT, "level": "info"}
    n = notify.Notifier([mail()])
    assert n.send(quiet, None, wait=True) == 0 and not smtp.log
    assert n.send(quiet, None, wait=True, every=True) == 1


def test_a_broken_mail_server_never_raises_and_says_why_in_the_test(smtp, monkeypatch):
    smtp.fail_login = True
    assert notify.Notifier([mail()]).send(EVENT, PHOTO, wait=True) == 0
    r = notify.send_test(mail())
    assert not r["ok"] and "username or password" in r["detail"] and "hunter2" not in r["detail"]

    def boom(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(notify.smtplib, "SMTP", boom)
    assert not notify.send_test(mail())["ok"]


def test_a_subject_cannot_carry_a_line_break():
    msg = notify.build_email(mail(), "Farm: a\r\nBcc: x@evil.example plate", EVENT, None)
    assert "\n" not in msg["Subject"] and msg["Bcc"] is None


# ---- Telegram: a local server answers like the Bot API ----
@pytest.fixture
def telegram(monkeypatch):
    got: list = []
    reply = {"sendPhoto": (200, {"ok": True}), "sendMessage": (200, {"ok": True})}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            method = self.path.rsplit("/", 1)[1]
            got.append((self.path, self.headers["Content-Type"], body))
            code, doc = reply[method]
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(doc).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(notify, "TELEGRAM_API", f"http://127.0.0.1:{srv.server_port}")
    yield got, reply
    srv.shutdown()


def bot(**kw):
    return Notifier(kind="telegram", bot_token="123:SECRET", chat_id=-100555, **kw)


def test_telegram_sends_the_picture_with_the_text_as_its_caption(telegram):
    got, _ = telegram
    assert notify.Notifier([bot()], farm_name="Farm").send(EVENT, PHOTO, wait=True) == 1
    (path, ctype, body), = got
    assert path == "/bot123:SECRET/sendPhoto" and ctype.startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"\r\n\r\n-100555' in body
    assert b'name="caption"\r\n\r\nFarm: p2s plate not clear\nParts left on the plate.\nCheck it.' in body
    assert b'filename="plate.jpg"' in body and PHOTO in body


def test_telegram_falls_back_to_text_when_the_photo_is_refused(telegram):
    got, reply = telegram
    reply["sendPhoto"] = (400, {"ok": False, "description": "Bad Request: wrong file"})
    assert notify.Notifier([bot()]).send(EVENT, PHOTO, wait=True) == 1
    assert [p.rsplit("/", 1)[1] for p, _, _ in got] == ["sendPhoto", "sendMessage"]
    doc = json.loads(got[1][2])
    assert doc["chat_id"] == "-100555" and doc["text"].endswith("Check it.")


def test_telegram_text_only_when_there_is_no_picture_and_a_long_caption_is_cut(telegram):
    got, _ = telegram
    assert notify.Notifier([bot()]).send(EVENT, None, wait=True) == 1
    assert [p.rsplit("/", 1)[1] for p, _, _ in got] == ["sendMessage"]
    long = {**EVENT, "message": "x" * 3000}
    (req,) = notify.requests_for(bot(), "T", long, None)
    assert len(json.loads(req.data)["text"]) <= 4096
    photo, _text = notify.requests_for(bot(), "T", long, PHOTO)
    assert b"x" * 1000 in photo.data and b"x" * 1100 not in photo.data  # the caption holds 1024 characters


def test_telegram_test_says_what_telegram_said_without_the_token(telegram):
    _, reply = telegram
    reply["sendMessage"] = (401, {"ok": False, "description": "Unauthorized"})
    r = notify.send_test(bot())
    assert not r["ok"] and "Unauthorized" in r["detail"] and "SECRET" not in r["detail"]
    reply["sendMessage"] = (200, {"ok": True})
    assert notify.send_test(bot())["ok"]


def test_telegram_respects_the_lowest_level(telegram):
    got, _ = telegram
    assert notify.Notifier([bot(min_level="error")]).send({**EVENT, "level": "warning"}, None, wait=True) == 0 and not got


# ---- what is saved, and what a page may see ----
def test_targets_must_be_complete():
    for bad in ({"kind": "email", "smtp_host": "h", "mail_to": []},
                {"kind": "email", "smtp_host": "h", "mail_from": "a@b.c", "mail_to": ["nobody"]},
                {"kind": "email", "mail_from": "a@b.c", "mail_to": ["a@b.c"]},
                {"kind": "telegram", "bot_token": "no-colon", "chat_id": "1"},
                {"kind": "telegram", "bot_token": "1:a"},
                {"kind": "ntfy"}):
        with pytest.raises(ValueError):
            Notifier.model_validate(bad)
    ok = Notifier.model_validate({"kind": "email", "smtp_host": "h", "mail_from": "a@b.c", "mail_to": "x@y.z; q@r.s"})
    assert ok.mail_to == ["x@y.z", "q@r.s"]


def test_a_page_never_sees_a_secret_and_a_phone_sees_less():
    cfgs = [mail(), bot(), Notifier(kind="webhook", url="https://hooks.example.com/abc", token="tok3n"),
            Notifier(kind="discord", url="https://discord.com/api/webhooks/1/WEBHOOKPATH")]
    for cfg in cfgs:
        for window in (True, False):
            shown = json.dumps(notify.public_view(cfg, window))
            for secret in ("hunter2", "SECRET", "tok3n"):  # the saved secrets, in the window and on a phone
                assert secret not in shown
    assert "WEBHOOKPATH" in json.dumps(notify.public_view(cfgs[3]))  # the window shows the address it will edit
    assert "WEBHOOKPATH" not in json.dumps(notify.public_view(cfgs[3], window=False))  # a phone doesn't
    window, phone = notify.public_view(cfgs[0]), notify.public_view(cfgs[0], window=False)
    assert window["has_smtp_password"] is True and window["mail_to"] == ["a@example.com", "b@example.com"]
    assert "mail_to" not in phone and "smtp_user" not in phone and phone["label"] == "mail.example.com"
    assert notify.public_view(cfgs[2])["has_token"] is True
    assert notify.public_view(cfgs[3], window=False)["url"] == "https://discord.com/…"


def test_only_web_addresses_are_sent_to():
    (cfg,) = [Notifier(kind="webhook", url="file:///etc/passwd")]
    assert notify.Notifier([cfg]).send(EVENT, None, wait=True) == 0


def test_a_telegram_bot_token_must_look_like_one():
    import pytest

    from proofloop.core.settings import Notifier

    Notifier(kind="telegram", bot_token="123456:ABC-def_g", chat_id="1")
    for bad in ("123456 :ABC", "123:AB C", "nocolon", "123:abc/../x"):
        with pytest.raises(ValueError):
            Notifier(kind="telegram", bot_token=bad, chat_id="1")
