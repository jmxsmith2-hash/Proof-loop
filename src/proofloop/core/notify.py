"""Notifications: ntfy, Discord webhooks, or any JSON webhook, set up once under Printers › Monitor.

Everything uses the same targets: printer events (``Notifier``, with each target's lowest level)
and a long check that has finished (``send``, to every target). A failed plate check sends the
camera picture along: ntfy takes it as a PUT body with ``Filename``/``Message`` headers, Discord as
a multipart upload with ``payload_json``. Both formats match Bambuddy's notification service.
Sending never raises: a failed notification is only logged.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)
LEVELS = {"info": 0, "warning": 1, "error": 2}


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
        title = f"{self.farm_name}: {event.get('printer') or ''} {event['kind'].replace('_', ' ')}".strip()
        try:
            for req in requests_for(cfg, title, event, image):
                try:
                    with urllib.request.urlopen(req, timeout=15):
                        return True
                except urllib.error.HTTPError as e:
                    # ntfy servers can disable attachments: fall back to the text-only request.
                    log.info("notification via %s refused (%s); trying without the picture", cfg.kind, e.code)
        except Exception as e:  # notifications must never break monitoring
            log.warning("notification via %s failed: %s", cfg.kind, e)
        return False


def requests_for(cfg, title: str, event: dict, image: bytes | None) -> list[urllib.request.Request]:
    """The request(s) to try, in order: with the picture first if there is one."""
    msg = event["message"]
    header_title = title.encode("latin-1", "replace").decode("latin-1")  # HTTP headers are Latin-1
    out: list[urllib.request.Request] = []
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
        payload = {"content": f"**{title}**\n{msg}"}
        if image:
            boundary = secrets.token_hex(12)
            payload["embeds"] = [{"image": {"url": "attachment://plate.jpg"}}]
            body = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                    f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n"
                    f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; filename="plate.jpg"\r\n'
                    "Content-Type: image/jpeg\r\n\r\n"
                ).encode()
                + image
                + f"\r\n--{boundary}--\r\n".encode()
            )
            req = urllib.request.Request(cfg.url, data=body, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
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
    if cfg.token:
        for r in out:
            r.add_header("Authorization", f"Bearer {cfg.token}")
    return out


def send(title: str, message: str, link: str | None = None) -> int:
    """Tell every target you set up that something you asked for is done. Returns how many took it."""
    from proofloop.core.paths import home
    from proofloop.core.settings import SettingsStore

    try:
        targets = SettingsStore(home()).load().notifications
    except Exception:  # noqa: BLE001 - unreadable settings: nobody to tell
        log.warning("could not read the notification targets", exc_info=True)
        return 0
    event = {"kind": "check_done", "level": "info", "message": message, "printer": None}
    if link:
        event["link"] = link
    sent = 0
    for cfg in targets:
        try:
            for req in requests_for(cfg, title, event, None):
                with urllib.request.urlopen(req, timeout=10) as r:
                    sent += int(200 <= r.status < 300)
                break
        except Exception as exc:  # noqa: BLE001 - a notification must never break a check
            log.warning("notification via %s failed: %s", cfg.kind, exc)
    return sent
