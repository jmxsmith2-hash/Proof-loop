"""Notifications: ntfy, Discord, Telegram, email, or any JSON webhook, set up once under Printers › Monitor.

Everything uses the same targets: printer events (``Notifier``, with each target's lowest level)
and a long check that has finished (``send``, to every target). A failed plate check sends the
camera picture along: ntfy takes it as a PUT body with ``Filename``/``Message`` headers, Discord as
a multipart upload with ``payload_json``, Telegram as a ``sendPhoto`` upload, email as a ``plate.jpg``
attachment. The ntfy and Discord formats match Bambuddy's notification service. A target that refuses the
picture gets the text alone. Sending never raises: a failed notification is only logged.

Secrets (``token``, ``smtp_password``, ``bot_token``) are kept in settings.json and never sent back to a
page: ``public_view`` is what a page may see.
"""

from __future__ import annotations

import json
import logging
import secrets
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
LEVELS = {"info": 0, "warning": 1, "error": 2}
SECRETS = ("token", "smtp_password", "bot_token")
TELEGRAM_API = "https://api.telegram.org"  # tests point this at a local server
SMTP_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


class Notifier:
    def __init__(self, configs, farm_name: str = "ProofLoop"):
        self.configs = list(configs)
        self.farm_name = farm_name

    def send(self, event: dict, image: bytes | None = None, wait: bool = False, every: bool = False) -> int:
        """Send to every target whose level allows it (``every``: to all of them), in the background. With
        ``wait`` (e.g. when ProofLoop is about to quit), wait up to 15 s and return how many targets took it."""
        done: list[bool] = []
        threads = []
        for cfg in self.configs:
            if every or LEVELS.get(event["level"], 0) >= LEVELS[cfg.min_level]:
                t = threading.Thread(target=lambda c=cfg: done.append(self._deliver(c, event, image)), daemon=True)
                t.start()
                threads.append(t)
        if wait:
            for t in threads:
                t.join(timeout=15)
        return sum(done)

    def _deliver(self, cfg, event: dict, image: bytes | None) -> bool:
        return deliver(cfg, title_of(event, self.farm_name), event, image)


def title_of(event: dict, farm_name: str = "ProofLoop") -> str:
    return f"{farm_name}: {event.get('printer') or ''} {event['kind'].replace('_', ' ')}".strip()


def deliver(cfg, title: str, event: dict, image: bytes | None, timeout: float = 15, problems: list[str] | None = None) -> bool:
    """Send one notice to one target; True if it took it. Never raises. ``problems`` collects what went wrong,
    in plain words and with no secrets in them (for the "Send test" button)."""
    why = problems if problems is not None else []
    try:
        if cfg.kind == "email":
            return _email(cfg, title, event, image, timeout)
        for req in requests_for(cfg, title, event, image):
            try:
                with urllib.request.urlopen(req, timeout=timeout):
                    return True
            except urllib.error.HTTPError as e:
                # ntfy servers can disable attachments, Telegram can refuse a picture: fall back to the text.
                log.info("notification via %s refused (%s); trying without the picture", cfg.kind, e.code)
                why.append(_refusal(cfg.kind, e))
    except Exception as e:  # noqa: BLE001 - a notice must never break monitoring, whatever the target does
        log.warning("notification via %s failed (%s)", cfg.kind, type(e).__name__, exc_info=True)
        why.append(explain(e))
    return False


def _refusal(kind: str, e: urllib.error.HTTPError) -> str:
    said = ""
    try:
        said = str(json.loads(e.read(2000)).get("description") or "")  # Telegram says what is wrong
    except (ValueError, AttributeError, OSError):
        pass
    return f"{kind} refused it ({e.code}{': ' + said if said else ''})"


def explain(e: BaseException) -> str:
    """What went wrong, in a few plain words (never includes a URL or a password)."""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "the mail server refused the username or password"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "the mail server refused the address to send to"
    if isinstance(e, smtplib.SMTPSenderRefused):
        return "the mail server refused the From address"
    if isinstance(e, smtplib.SMTPServerDisconnected):
        return "the mail server closed the connection: check the port and the security setting"
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return "the mail server doesn't support this: try another security setting"
    if isinstance(e, smtplib.SMTPException):
        return f"the mail server said no ({e.__class__.__name__})"
    if isinstance(e, ssl.SSLError):
        return "the secure connection failed: check the port and the security setting"
    if isinstance(e, TimeoutError):
        return "no answer (timed out)"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused: check the address and port"
    if isinstance(e, socket.gaierror):
        return "can't find that address"
    if isinstance(e, urllib.error.URLError):
        return explain(e.reason) if isinstance(e.reason, BaseException) else f"can't reach it ({e.reason})"
    if isinstance(e, OSError):
        return f"network problem ({e.strerror or e.__class__.__name__})"
    if isinstance(e, ValueError):
        return str(e)
    return e.__class__.__name__


def multipart(parts: list[tuple[str, str | None, str | None, bytes]]) -> tuple[bytes, str]:
    """A multipart/form-data body from ``(field name, file name, content type, bytes)`` and its content type."""
    boundary = secrets.token_hex(12)
    body = b""
    for name, filename, ctype, data in parts:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
        if filename:
            head += f'; filename="{filename}"'
        head += "\r\n" + (f"Content-Type: {ctype}\r\n" if ctype else "") + "\r\n"
        body += head.encode() + data + b"\r\n"
    return body + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def requests_for(cfg, title: str, event: dict, image: bytes | None) -> list[urllib.request.Request]:
    """The request(s) to try, in order: with the picture first if there is one."""
    msg = event["message"]
    header_title = title.encode("latin-1", "replace").decode("latin-1")  # HTTP headers are Latin-1
    out: list[urllib.request.Request] = []
    if cfg.kind == "telegram":
        return _telegram(cfg, title, event, image)
    if not str(cfg.url).startswith(("http://", "https://")):
        raise ValueError("the URL should start with http:// or https://")
    if cfg.kind == "ntfy":
        prio = {"error": "urgent", "warning": "high"}.get(event["level"], "default")
        if image:
            req = urllib.request.Request(cfg.url, data=image, method="PUT")
            req.add_header("Filename", "plate.jpg")
            req.add_header("Message", msg.replace("\n", "\\n"))
            req.add_header("Title", header_title)
            req.add_header("Priority", prio)
            out.append(req)
        req = urllib.request.Request(cfg.url, data=msg.encode(), method="POST")
        req.add_header("Title", header_title)
        req.add_header("Priority", prio)
        if str(event.get("link") or "").startswith("http"):
            req.add_header("Click", event["link"])
        out.append(req)
    elif cfg.kind == "discord":
        payload: dict[str, Any] = {"content": f"**{title}**\n{msg}"}
        if image:
            payload["embeds"] = [{"image": {"url": "attachment://plate.jpg"}}]
            body, ctype = multipart([
                ("payload_json", None, "application/json", json.dumps(payload).encode()),
                ("files[0]", "plate.jpg", "image/jpeg", image),
            ])
            req = urllib.request.Request(cfg.url, data=body, method="POST")
            req.add_header("Content-Type", ctype)
            out.append(req)
        req = urllib.request.Request(
            cfg.url, data=json.dumps({"content": f"**{title}**\n{msg}"}).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        out.append(req)
    else:
        req = urllib.request.Request(
            cfg.url, data=json.dumps({"title": title, **event}).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        out.append(req)
    if getattr(cfg, "token", None):
        for r in out:
            r.add_header("Authorization", f"Bearer {cfg.token}")
    return out


def _telegram(cfg, title: str, event: dict, image: bytes | None) -> list[urllib.request.Request]:
    """``sendPhoto`` (a multipart upload; a caption holds up to 1024 characters) then ``sendMessage`` (up to 4096)."""
    api = f"{TELEGRAM_API}/bot{cfg.bot_token}"
    text = f"{title}\n{event['message']}"
    if str(event.get("link") or "").startswith("http"):
        text += f"\n{event['link']}"
    out: list[urllib.request.Request] = []
    if image:
        caption = text if len(text) <= 1024 else text[:1023] + "…"
        body, ctype = multipart([
            ("chat_id", None, None, str(cfg.chat_id).encode()),
            ("caption", None, None, caption.encode()),
            ("photo", "plate.jpg", "image/jpeg", image),
        ])
        req = urllib.request.Request(f"{api}/sendPhoto", data=body, method="POST")
        req.add_header("Content-Type", ctype)
        out.append(req)
    req = urllib.request.Request(
        f"{api}/sendMessage",
        data=json.dumps({"chat_id": cfg.chat_id, "text": text[:4096]}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    out.append(req)
    return out


def build_email(cfg, title: str, event: dict, image: bytes | None) -> EmailMessage:
    """The email: the title as its subject, the message as its body, the camera picture as ``plate.jpg``."""
    m = EmailMessage()
    m["Subject"] = " ".join(title.split())  # one line: a header can't hold a line break
    m["From"] = cfg.mail_from
    m["To"] = ", ".join(cfg.mail_to)
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="proofloop.local")
    body = event["message"]
    if str(event.get("link") or "").startswith("http"):
        body += f"\n\n{event['link']}"
    m.set_content(body)
    if image:
        m.add_attachment(image, maintype="image", subtype="jpeg", filename="plate.jpg")
    return m


def _email(cfg, title: str, event: dict, image: bytes | None, timeout: float) -> bool:
    for pic in ([image, None] if image else [None]):
        try:
            _smtp_send(cfg, build_email(cfg, title, event, pic), timeout)
            return True
        except smtplib.SMTPDataError as e:
            if pic is None:
                raise
            log.info("mail server refused the message (%s); trying without the picture", e.smtp_code)
    return False


def _smtp_send(cfg, msg: EmailMessage, timeout: float) -> None:
    port = cfg.smtp_port or SMTP_PORTS[cfg.smtp_security]
    ctx = ssl.create_default_context()
    smtp = (
        smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=timeout, context=ctx)
        if cfg.smtp_security == "ssl"
        else smtplib.SMTP(cfg.smtp_host, port, timeout=timeout)
    )
    with smtp:
        if cfg.smtp_security == "starttls":
            smtp.starttls(context=ctx)
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password or "")
        smtp.send_message(msg)


def send_test(cfg, farm_name: str = "ProofLoop") -> dict:
    """Send a test notice to one target, whatever its level: ``{"ok": bool, "detail": str}``."""
    event = {"ts": time.time(), "printer": None, "level": "error", "kind": "test",
             "message": "Test notice from ProofLoop: this target works."}
    problems: list[str] = []
    ok = deliver(cfg, title_of(event, farm_name), event, None, problems=problems)
    if ok:
        return {"ok": True, "detail": "Sent. Check that it arrived."}
    return {"ok": False, "detail": ("; ".join(dict.fromkeys(problems)) or "it didn't go through") + "."}


def label(cfg) -> str:
    """A short, secret-free name for a target: the host it goes to."""
    if cfg.kind == "email":
        return cfg.smtp_host or "email"
    if cfg.kind == "telegram":
        return "Telegram"
    return urlsplit(cfg.url).hostname or cfg.kind


def public_view(cfg, window: bool = True) -> dict:
    """What a page may see of a target: never a secret, only whether one is saved (``has_token``...).
    A phone gets even less: no address, user name or chat id, just the kind, level and the host."""
    d = cfg.model_dump(exclude=set(SECRETS))
    for f in SECRETS:
        d[f"has_{f}"] = bool(getattr(cfg, f))
    d["label"] = label(cfg)
    if not window:
        d = {k: v for k, v in d.items() if k in ("kind", "min_level", "label") or k.startswith("has_")}
        if cfg.url:
            parts = urlsplit(cfg.url)
            d["url"] = f"{parts.scheme}://{parts.hostname or ''}/…"
    return d


def send(title: str, message: str, link: str | None = None) -> int:
    """Tell every target you set up that something you asked for is done. Returns how many took it."""
    from proofloop.core.paths import home
    from proofloop.core.settings import SettingsStore

    try:
        targets = SettingsStore(home()).load().notifications
    except (OSError, ValueError):  # unreadable or damaged settings: nobody to tell
        log.warning("could not read the notification targets", exc_info=True)
        return 0
    event = {"kind": "check_done", "level": "info", "message": message, "printer": None}
    if link:
        event["link"] = link
    return sum(deliver(cfg, title, event, None, timeout=10) for cfg in targets)
