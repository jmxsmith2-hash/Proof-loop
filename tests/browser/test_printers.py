"""Printers: the pretend printers on Overview and Monitor, filament and orders."""

import re
import time
import uuid

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.browser.window import EXAMPLES, PRETEND_PRINTERS  # noqa: E402

ONLINE = re.compile(r"^(online|waiting for you)$")  # the pretend printers are connected; one may want its plate cleared


def unique(word: str) -> str:
    return f"{word} {uuid.uuid4().hex[:6]}"


def test_overview_lists_the_pretend_printers_with_their_status(window):
    page = window.tab("printers", "overview")
    runs = page.locator("#ovRuns .run")
    expect(runs).to_have_count(len(PRETEND_PRINTERS))
    for name in PRETEND_PRINTERS:
        run = runs.filter(has_text=name)
        expect(run).to_have_count(1)
        expect(run.locator(".chip")).to_have_text(ONLINE)
        expect(run.locator(".hint").first).not_to_be_empty()  # "printing ... layer 7/120", or what it waits for
    expect(page.locator("#ovKpis .kpi")).to_have_count(6)
    expect(page.locator("#ovSummary")).to_contain_text("While you were away")
    expect(page.locator("#queueOut")).not_to_be_empty()  # `proofloop try` starts a run of 60 brackets


def test_monitor_shows_each_printer_and_follows_it_live(window):
    page = window.tab("printers", "monitor")
    cards = page.locator("#printerCards > .card")
    expect(cards).to_have_count(len(PRETEND_PRINTERS))
    for name in PRETEND_PRINTERS:
        card = cards.filter(has=page.get_by_role("heading", name=name))
        expect(card).to_have_count(1)
        expect(card.locator(".chip").first).to_have_text(ONLINE)
        expect(card.locator("dl.kv")).to_contain_text("Job")
        expect(card.get_by_role("button", name="Test connection")).to_be_visible()
    expect(page.locator("#outcomes")).to_be_visible()
    expect(page.locator("#events")).to_contain_text("Pretend P2S")  # connecting is in the event log

    # live: the printers keep printing, and the page follows without being reloaded
    before = cards.all_inner_texts()
    end = time.time() + 30
    while cards.all_inner_texts() == before:
        assert time.time() < end, "the printer cards didn't change in 30 s"
        window.page.wait_for_timeout(250)


def test_the_filament_page_lists_the_spools_and_adds_one(window):
    page = window.tab("printers", "filament")
    spools = page.locator("#spoolList")
    expect(page.locator("#filKpis .kpi")).to_have_count(6)
    expect(spools).to_contain_text("PLA Basic")  # the farm's spools: three PLA, one PETG
    expect(spools).to_contain_text("PETG HF")
    expect(page.locator("#amsOut")).to_contain_text(PRETEND_PRINTERS[0])  # what each AMS holds, live

    name = unique("Playwright ASA")
    page.locator("#filAdd").click()
    expect(page.locator("#edTitle")).to_have_text("Add a spool")
    form = page.locator("#spoolForm")
    form.locator("[name=material]").fill("ASA")
    form.locator("[name=name]").fill(name)
    form.locator("[name=brand]").fill("Playwright")
    form.locator("[name=price]").fill("25")
    form.locator("[name=net_g]").fill("1000")
    form.locator("[name=tare_g]").fill("250")
    page.locator("#edSave").click()

    spool = spools.locator(":scope > *", has_text=name)
    expect(spool.first).to_be_visible()
    expect(spool.first).to_contain_text("ASA")
    expect(spool.first).to_contain_text("1000 g of 1000 g")  # new and sealed: it holds what the label says


def test_orders_take_an_order_for_the_example_plate(window):
    page = window.tab("printers", "orders")
    expect(page.locator("#orderAdd")).to_be_disabled()  # no plate yet
    page.locator("#orderFile").set_input_files(EXAMPLES / "bracket_plate.gcode.3mf")
    expect(page.locator("#orderPlate")).to_contain_text("bracket_plate.gcode.3mf · 2 parts per plate")
    expect(page.locator("#orderAdd")).to_be_enabled()

    customer = unique("Playwright Co")
    form = page.locator("#orderForm")
    form.locator("[name=customer]").fill(customer)
    form.locator("[name=quantity]").fill("6")
    form.locator("[name=notes]").fill("taken by a browser test")
    page.locator("#orderAdd").click()

    expect(page.locator(".toast")).to_contain_text(f"Order for {customer} taken")
    order = page.locator("#ordersOut .run", has_text=customer)
    expect(order).to_have_count(1)
    expect(order).to_contain_text("6 × bracket_plate.gcode.3mf")
    expect(order).to_contain_text("0 of 6 made")
    expect(order).to_contain_text("taken by a browser test")
    expect(order.get_by_role("button", name="Cancel")).to_be_visible()
    expect(page.locator("#orderAdd")).to_be_disabled()  # the form is ready for the next order
