"""Tell you when a long check finishes: ntfy, Discord or any JSON webhook.

Where to send comes from SYNTHUX_NOTIFY_URL (an ntfy topic URL, a Discord webhook URL, or any URL
that takes a JSON POST), or else from the notifications set up in ClearLoop
(~/.clearloop/settings.json), so phone alerts set up once work for both. Sending never raises:
a failed notification is only logged.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from synthux.core.logging import get_logger

log = get_logger(__name__)
CLEARLOOP_SETTINGS = Path.home() / ".clearloop" / "settings.json"


def targets() -> list[dict[str, Any]]:
    url = os.environ.get("SYNTHUX_NOTIFY_URL", "").strip()
    if url:
        kind = "discord" if "discord.com/api/webhooks" in url else "ntfy" if "ntfy" in url else "webhook"
        return [{"kind": kind, "url": url, "token": os.environ.get("SYNTHUX_NOTIFY_TOKEN")}]
    try:
        data = json.loads(CLEARLOOP_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [n for n in data.get("notifications") or [] if isinstance(n, dict) and n.get("url")]


def _request(t: dict[str, Any], title: str, message: str, link: str | None) -> urllib.request.Request:
    headers = {"User-Agent": "synthux"}
    if t.get("token"):
        headers["Authorization"] = f"Bearer {t['token']}"
    if t["kind"] == "ntfy":
        headers |= {"Title": title.encode("utf-8").decode("latin-1", "replace"), "Tags": "printer"}
        if link and link.startswith("http"):
            headers["Click"] = link
        return urllib.request.Request(t["url"], data=message.encode("utf-8"), headers=headers, method="POST")
    body = {"content": f"**{title}**\n{message}"} if t["kind"] == "discord" else \
        {"title": title, "message": message, "link": link, "source": "synthux"}
    headers["Content-Type"] = "application/json"
    return urllib.request.Request(t["url"], data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")


def send(title: str, message: str, link: str | None = None) -> int:
    """Send to every target; returns how many accepted it."""
    sent = 0
    for t in targets():
        try:
            with urllib.request.urlopen(_request(t, title, message, link), timeout=10) as r:
                sent += int(200 <= r.status < 300)
        except Exception as exc:  # noqa: BLE001 - a notification must never break a check
            log.warning("notification via %s failed: %s", t.get("kind"), exc)
    return sent
