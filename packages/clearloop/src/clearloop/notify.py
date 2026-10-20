"""Push notifications: ntfy, Discord webhooks, or any JSON webhook."""

from __future__ import annotations

import json
import logging
import threading
import urllib.request

log = logging.getLogger(__name__)
LEVELS = {"info": 0, "warning": 1, "error": 2}


class Notifier:
    def __init__(self, configs, farm_name: str = "ClearLoop"):
        self.configs = list(configs)
        self.farm_name = farm_name

    def send(self, event: dict) -> None:
        for cfg in self.configs:
            if LEVELS.get(event["level"], 0) >= LEVELS[cfg.min_level]:
                threading.Thread(target=self._deliver, args=(cfg, event), daemon=True).start()

    def _deliver(self, cfg, event: dict) -> None:
        title = f"{self.farm_name}: {event.get('printer') or 'farm'} {event['kind']}"
        try:
            req = _request(cfg, title, event)
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as e:  # notifications must never break the loop
            log.warning("notification via %s failed: %s", cfg.kind, e)


def _request(cfg, title: str, event: dict) -> urllib.request.Request:
    if cfg.kind == "ntfy":
        req = urllib.request.Request(cfg.url, data=event["message"].encode(), method="POST")
        req.add_header("Title", title)
        req.add_header("Priority", {"error": "urgent", "warning": "high"}.get(event["level"], "default"))
        req.add_header(
            "Tags", {"error": "rotating_light", "warning": "warning"}.get(event["level"], "printer")
        )
    elif cfg.kind == "discord":
        body = {"content": f"**{title}**\n{event['message']}"}
        req = urllib.request.Request(cfg.url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(
            cfg.url, data=json.dumps({"title": title, **event}).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
    if cfg.token:
        req.add_header("Authorization", f"Bearer {cfg.token}")
    return req
