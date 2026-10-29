"""Action guard: checks what a click or Enter would hit before it happens.

The guard reads the element under the pointer from the page. That information is
used only here, by code; it is never shown to the persona model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from proofloop.core.config import SafetyConfig
from proofloop.core.logging import get_logger

log = get_logger(__name__)

# JavaScript run in the page to describe the element at (x, y) and, for Enter,
# the submit controls of the focused element's form.
ELEMENT_PROBE_JS = """
([x, y, forEnter]) => {
  const describe = (el) => el ? {
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || '').slice(0, 200),
    type: el.getAttribute('type') || '',
    name: el.getAttribute('name') || '',
    id: el.id || '',
    aria: el.getAttribute('aria-label') || '',
    href: el.getAttribute('href') || '',
    formAction: el.form ? (el.form.getAttribute('action') || '') : ''
  } : null;
  let el = forEnter ? document.activeElement : document.elementFromPoint(x, y);
  const clickable = el && el.closest ? (el.closest('button, a, input, [role=button], label, select, textarea') || el) : el;
  const info = describe(clickable) || {};
  if (forEnter && el && el.form) {
    info.submitTexts = Array.from(el.form.querySelectorAll('button, input[type=submit]'))
      .map(b => (b.innerText || b.value || '').slice(0, 80));
  }
  return info;
}
"""


@dataclass
class GuardResult:
    allowed: bool
    category: str = ""   # payment | email | destructive
    reason: str = ""
    element: dict[str, Any] = field(default_factory=dict)


class ActionGuard:
    def __init__(self, safety: SafetyConfig, *, sandbox: bool):
        self.sandbox = sandbox
        self._patterns = {
            "payment": [re.compile(p, re.I) for p in safety.payment_patterns],
            "email": [re.compile(p, re.I) for p in safety.email_patterns],
            "destructive": [re.compile(p, re.I) for p in safety.destructive_patterns],
        }

    def classify(self, element: dict[str, Any]) -> tuple[str, str] | None:
        """Return (category, matched text) if the element looks risky."""
        texts = [str(element.get(k, "")) for k in ("text", "aria", "name", "id", "href", "formAction")]
        texts += [str(t) for t in element.get("submitTexts", [])]
        haystack = " | ".join(t for t in texts if t)
        for category, patterns in self._patterns.items():
            for pattern in patterns:
                if pattern.search(haystack):
                    return category, pattern.pattern
        return None

    def check(self, action_kind: str, element: dict[str, Any]) -> GuardResult:
        if action_kind not in ("click", "press_enter"):
            return GuardResult(True, element=element)
        hit = self.classify(element)
        if hit is None:
            return GuardResult(True, element=element)
        category, pattern = hit
        if self.sandbox:
            log.debug("allowing %s action on sandbox target (%s)", category, pattern)
            return GuardResult(True, category=category, element=element)
        reason = f"blocked {category} action on a non-sandbox target (matched {pattern!r})"
        log.warning(reason)
        return GuardResult(False, category=category, reason=reason, element=element)
