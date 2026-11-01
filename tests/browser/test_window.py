"""The window itself: its tabs, its menu, the guards on what only the window may do, and a quiet console."""

import json
import re
import urllib.error
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.browser.window import REQUIRED_PAGES  # noqa: E402


def test_the_window_opens_on_check_and_switches_tabs(window):
    page = window.page
    expect(page).to_have_title("ProofLoop")
    expect(page.locator("#ver")).to_have_text(re.compile(r"^v\d+\.\d+"))
    expect(page.locator("#pretendBanner")).to_be_visible()  # `proofloop try` says it is a pretend farm
    tabs = page.get_by_role("tab")
    expect(tabs).to_have_text(["Check", "Loop files", "Printers"])
    expect(page.locator("#check")).to_be_visible()
    expect(page.locator("#loop")).to_be_hidden()

    page.get_by_role("tab", name="Loop files").click()
    expect(page.get_by_role("tab", name="Loop files")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tab", name="Check")).to_have_attribute("aria-selected", "false")
    expect(page.locator("#loop")).to_be_visible()
    expect(page.locator("#check")).to_be_hidden()
    expect(window.loop.get_by_role("heading", name="Make a loop file")).to_be_visible()

    page.get_by_role("tab", name="Printers").click()
    expect(page.get_by_role("tab", name="Printers")).to_have_attribute("aria-selected", "true")
    expect(window.loop.get_by_role("heading", name="Overview")).to_be_visible()  # the same page, the Printers pages

    page.get_by_role("tab", name="Check").click()
    expect(page.locator("#check")).to_be_visible()
    expect(page.locator("#loop")).to_be_hidden()


def test_the_tabs_work_from_the_keyboard_and_are_remembered(window):
    page = window.page
    page.locator("#tab-check").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#tab-loopfiles")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#tab-loopfiles")).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#tab-printers")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowRight")  # wraps around
    expect(page.locator("#tab-check")).to_have_attribute("aria-selected", "true")

    page.get_by_role("tab", name="Printers").click()
    page.reload()
    expect(page.locator("#tab-printers")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#loop")).to_be_visible()


def test_loop_files_and_printers_each_show_their_own_pages(window):
    for part, required in REQUIRED_PAGES.items():
        pages = window.pages(part)
        assert set(required) <= set(pages), pages
        expect(window.loop.locator("nav [role=tab]:visible")).to_have_count(len(pages))  # not the other tab's
        for tab in pages:
            window.tab(part, tab)
            expect(window.loop.locator(f'nav [data-tab="{tab}"]')).to_have_attribute("aria-selected", "true")
            expect(window.loop.locator("[data-panel]:visible")).to_have_count(1)


def test_the_more_menu_opens_the_guide_and_the_news(window):
    """These talk to the window-only endpoints, so they only work with the window's own header."""
    page = window.page
    page.locator("#moreBtn").click()
    expect(page.locator("#moreMenu")).to_be_visible()
    page.locator("#guideBtn").click()
    expect(page.locator("#guide")).to_be_visible()
    expect(page.locator("#guideSteps li").first).to_be_visible()  # the steps came from /proofloop/guide
    page.locator("#guideClose").click()
    expect(page.locator("#guide")).to_be_hidden()

    page.locator("#moreBtn").click()
    page.locator("#newsBtn").click()
    expect(page.locator("#newsBody h3").first).to_be_visible()  # /proofloop/changelog
    page.keyboard.press("Escape")


def test_no_console_errors_on_any_page(window):
    page = window.page
    window.tab("check")
    for part in REQUIRED_PAGES:
        for tab in window.pages(part):
            window.tab(part, tab)
            page.wait_for_timeout(300)  # let the page's own requests finish
    window.tab("check")
    page.wait_for_timeout(300)
    window.assert_clean()


def test_window_only_requests_are_refused_without_the_windows_header(farm):
    """The guards stay as they are: a program that doesn't send the window's header gets a 403."""

    def status(path, headers=None, data=None):
        req = urllib.request.Request(farm.url + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    assert status("/proofloop/status") == 403
    assert status("/proofloop/status", {"X-ProofLoop": "1"}) == 200
    post = {"Content-Type": "application/json"}
    assert status("/proofloop/phone", post, json.dumps({"on": False}).encode()) == 403
    assert status("/check/api/check?name=x.gcode&kind=gcode", post, b"G1") == 403
    # another site's page can't make the window's requests either
    assert status("/proofloop/status", {"X-ProofLoop": "1", "Origin": "http://evil.example"}) == 403
    # and the server only answers to its own host names
    assert status("/", {"Host": "evil.example"}) == 400
