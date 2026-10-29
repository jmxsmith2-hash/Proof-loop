"""URL allowlist: the tool refuses to open anything not explicitly allowed."""

from __future__ import annotations

import fnmatch
from urllib.parse import urlsplit

from proofloop.core.config import AllowedHost
from proofloop.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_PORTS = {"http": 80, "https": 443}
SAFE_INTERNAL = ("about:blank",)


class SafetyError(PermissionError):
    """Raised when an action or URL is refused by the safety sandbox."""


def match_host(url: str, allowlist: list[AllowedHost]) -> AllowedHost | None:
    """Return the allowlist entry covering this URL, or None."""
    parts = urlsplit(url)
    if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    try:
        port = parts.port or DEFAULT_PORTS[parts.scheme]
    except ValueError:
        return None
    for entry in allowlist:
        pattern = entry.host.lower()
        host_ok = host == pattern or (("*" in pattern) and fnmatch.fnmatchcase(host, pattern))
        if host_ok and (not entry.ports or port in entry.ports):
            return entry
    return None


def check_url(url: str, allowlist: list[AllowedHost]) -> AllowedHost:
    """Raise SafetyError unless the URL is on the allowlist."""
    entry = match_host(url, allowlist)
    if entry is None:
        log.warning("refused URL not on allowlist: %s", url)
        raise SafetyError(
            f"{url} is not on the safety allowlist (config/settings.yaml -> safety.allowlist). "
            "Only staging or test environments should be listed there."
        )
    return entry


def is_internal(url: str) -> bool:
    return url in SAFE_INTERNAL or url.startswith(("data:", "blob:", "chrome-error:"))
