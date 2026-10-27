"""Network policy applied to every browser request."""

from __future__ import annotations

import re
from dataclasses import dataclass

from synthux.core.config import SafetyConfig
from synthux.core.logging import get_logger
from synthux.safety.allowlist import is_internal, match_host

log = get_logger(__name__)


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""


class RequestPolicy:
    """Decides whether a browser request may go out.

    * Navigations (top-level documents) must stay on the allowlist.
    * Writes (non-GET) to hosts off the allowlist are always blocked.
    * Payment / email provider URLs are blocked unless the target is a sandbox.
    """

    def __init__(self, safety: SafetyConfig, *, sandbox: bool):
        self.safety = safety
        self.sandbox = sandbox
        self._blocked = [re.compile(p, re.I) for p in safety.blocked_request_patterns]
        self.blocked_log: list[dict[str, str]] = []

    def decide(self, url: str, method: str = "GET", *, is_navigation: bool = False) -> Decision:
        if is_internal(url):
            return Decision(True)
        on_list = match_host(url, self.safety.allowlist) is not None
        if is_navigation and not on_list:
            return self._block(url, method, "navigation off the allowlist")
        if method.upper() != "GET" and not on_list:
            return self._block(url, method, "write request to a host off the allowlist")
        if not self.sandbox:
            for pattern in self._blocked:
                if pattern.search(url):
                    return self._block(url, method, f"matches blocked pattern {pattern.pattern!r}")
        return Decision(True)

    def _block(self, url: str, method: str, reason: str) -> Decision:
        log.warning("blocked %s %s: %s", method, url, reason)
        self.blocked_log.append({"url": url, "method": method, "reason": reason})
        return Decision(False, reason)
