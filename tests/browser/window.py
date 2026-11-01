"""Driving the ProofLoop window the way a person does: its tabs, and the two pages inside it."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import FrameLocator, Page, expect

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples" / "gcode"
PRETEND_PRINTERS = ("Pretend P2S 1", "Pretend P2S 2", "Pretend P2S 3")
REQUIRED_PAGES = {  # window tab -> pages of Loop files & Printers it must show (it may show more)
    "loopfiles": ("make", "check", "costs", "calibrate", "profiles"),
    "printers": ("overview", "monitor", "filament", "orders"),
}


class Window:
    """``shell.html`` with the Check page (``check``) and Loop files & Printers (``loop``) in its frames."""

    def __init__(self, page: Page, url: str):
        self.page = page
        self.url = url
        self.problems: list[str] = []
        self.expected: list[str] = []
        self.check: FrameLocator = page.frame_locator("#check")
        self.loop: FrameLocator = page.frame_locator("#loop")
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.problems.append(f"uncaught exception: {e}"))

    def _console(self, msg) -> None:
        if msg.type == "error":
            where = msg.location or {}
            self.problems.append(f"console error: {msg.text} ({where.get('url')}:{where.get('lineNumber')})")

    def expect_console_error(self, text: str) -> None:
        """A test that provokes an error (a refused upload logs the failed request) says so here."""
        self.expected.append(text)

    def assert_clean(self) -> None:
        left = [p for p in self.problems if not any(e in p for e in self.expected)]
        assert not left, "the pages logged errors:\n" + "\n".join(left)

    # ---- opening -------------------------------------------------------------------
    def open(self) -> None:
        """The window, with both frames loaded. A fresh browser context starts on Check."""
        self.page.goto(self.url + "/")
        expect(self.page.locator("#tab-check")).to_have_attribute("aria-selected", "true")
        expect(self.check.locator("#g-drop")).to_be_visible()
        # Loop files & Printers loads its state, then picks its first tab; wait for that before clicking in it.
        expect(self.loop.locator("#ver")).to_have_text(re.compile(r"^v\d"))

    def pages(self, part: str) -> list[str]:
        """The pages of ``loopfiles`` or ``printers`` as the page lists them (so a new one is visited too)."""
        self.page.locator(f"#tab-{part}").click()
        group = self.loop.locator(f'nav [data-part="{part}"] [role=tab]')
        expect(group.first).to_be_visible()
        return [group.nth(i).get_attribute("data-tab") for i in range(group.count())]

    def tab(self, part: str, tab: str | None = None) -> FrameLocator:
        """Open a window tab (``check``, ``loopfiles`` or ``printers``) and, for the last two, one of its
        pages. Returns the frame that shows it."""
        self.page.locator(f"#tab-{part}").click()
        expect(self.page.locator(f"#tab-{part}")).to_have_attribute("aria-selected", "true")
        if part == "check":
            expect(self.check.locator("#g-drop")).to_be_visible()
            return self.check
        button = self.loop.locator(f'nav [role=tab][data-tab="{tab}"]')
        expect(button).to_be_visible()  # the other tab's buttons are hidden
        button.click()
        expect(self.loop.locator(f'[data-panel="{tab}"]')).to_be_visible()
        return self.loop
