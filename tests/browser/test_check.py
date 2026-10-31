"""Check: sliced G-code goes in, a verdict comes out."""

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.browser.window import EXAMPLES  # noqa: E402


def check_gcode(window, name: str):
    """Give Check a G-code example the way a person does (the file chooser behind the drop zone) and
    return the finished job's card."""
    check = window.tab("check")
    jobs = window.page.request.get(window.url + "/check/api/jobs")
    known = {j["id"] for j in jobs.json()}  # other tests' jobs are on the page too
    with window.page.expect_response(lambda r: r.request.method == "POST" and "/check/api/check?" in r.url) as sent:
        check.locator("#g-file").set_input_files(EXAMPLES / name)
    assert sent.value.status == 201
    (job_id,) = {j["id"] for j in window.page.request.get(window.url + "/check/api/jobs").json()} - known
    done = check.locator(".job", has=window.page.locator(f'button[data-remove="{job_id}"]'))  # only when finished
    expect(done).to_be_visible(timeout=60_000)
    return done


def test_check_accepts_clean_gcode_and_says_so(window):
    job = check_gcode(window, "bracket_good.gcode")
    expect(job.get_by_role("heading")).to_have_text("G-code: bracket_good.gcode")
    expect(job.locator(".chip")).to_have_text("done")
    expect(job).to_contain_text("no problems found")
    expect(job).to_contain_text("Bambu Lab P2S, PLA")
    expect(job.get_by_role("link", name="Open the report")).to_have_attribute("href", re.compile(r"^/check/job/\w+/$"))


def test_check_reports_the_problems_in_flawed_gcode(window):
    job = check_gcode(window, "bracket_flawed.gcode")
    expect(job.get_by_role("heading")).to_have_text("G-code: bracket_flawed.gcode")
    expect(job.locator(".chip")).to_have_text("done")
    counts = re.search(r"(\d+) major, (\d+) in total", job.inner_text())
    assert counts and int(counts.group(1)) > 0 and int(counts.group(2)) > 0, job.inner_text()
    expect(job.locator("ul li").first).to_be_visible()  # what was found


def test_check_refuses_a_file_it_cannot_read(window, tmp_path):
    window.expect_console_error("Failed to load resource")  # the refused upload, as the browser logs it
    check = window.tab("check")
    empty = tmp_path / "empty.gcode"
    empty.write_bytes(b"")
    check.locator("#g-file").set_input_files(empty)
    expect(check.locator("#g-err")).not_to_be_empty()
