"""Paid orders, invoice numbers that never change, the customer summary, and orders written before any of it."""

import json
from datetime import date

import pytest

from proofloop.core import schema
from proofloop.monitor.orders import Orders, customers, order_value

OLD = {"id": "o1", "customer": "Sam", "plate": "p.gcode.3mf", "quantity": 10, "due": None, "price_each": 2.0,
       "cost_each": None, "notes": "", "created": 1.0, "status": "done", "run": "r1", "parts_per_loop": 2,
       "loop_s": {"a": 60.0}, "told": True}


def orders(tmp_path, *rows):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps({"schema_version": 1, "orders": list(rows)}), encoding="utf-8")
    return Orders(path)


def test_an_order_from_before_paid_and_invoices_still_loads_and_is_unpaid(tmp_path):
    o = orders(tmp_path, OLD)
    assert "paid" not in o.get("o1") and "invoice" not in o.get("o1")
    o.set_paid("o1", True)
    got = o.get("o1")
    assert got["paid"] is True and got["paid_at"] > 0
    o.set_paid("o1", False)
    assert o.get("o1")["paid"] is False and o.get("o1")["paid_at"] is None
    assert json.loads((tmp_path / "orders.json").read_text(encoding="utf-8"))["schema_version"] == schema.current("orders")


def test_invoice_numbers_count_up_and_never_change(tmp_path):
    o = orders(tmp_path, OLD, {**OLD, "id": "o2"}, {**OLD, "id": "o3"})
    first, new = o.invoice("o1", "INV-", 1)
    assert new and first == {"number": 1, "label": "INV-0001", "date": date.today().isoformat()}
    again, new = o.invoice("o1", "OTHER-", 50)  # a new prefix or start doesn't touch an invoice already made
    assert not new and again == first
    second, _ = o.invoice("o2", "INV-", 1)  # 1 is taken: the next one up
    assert second["number"] == 2
    third, _ = o.invoice("o3", "A", 100)  # you can start counting higher
    assert third["number"] == 100 and third["label"] == "A0100"
    assert o.get("o1")["invoice"] == first
    with pytest.raises(KeyError):
        o.invoice("nope", "INV-", 1)


def test_an_invoice_number_survives_the_order_being_changed(tmp_path):
    o = orders(tmp_path, OLD)
    inv, _ = o.invoice("o1", "INV-", 7)
    o.update("o1", price_each=3.0, customer="Sam Ltd")
    assert o.get("o1")["invoice"] == inv


def test_the_customer_summary_counts_what_is_made_and_what_is_unpaid(tmp_path):
    rows = [
        {**OLD, "id": "a"},                                                     # made, unpaid: 20
        {**OLD, "id": "b", "paid": True, "customer": " sam "},                  # same customer, made, paid: 20
        {**OLD, "id": "c", "status": "open"},                                   # still to make: counts parts, not earned
        {**OLD, "id": "d", "status": "cancelled"},                              # left out
        {**OLD, "id": "e", "customer": "Ann", "price_each": None},              # no price: nothing earned
        {**OLD, "id": "f", "customer": "Zed", "quantity": 3, "price_each": 1.5, "paid": True},
    ]
    got = {c["customer"]: c for c in customers(rows)}
    assert got["Sam"] == {"customer": "Sam", "orders": 3, "parts": 30, "earned": 40.0, "unpaid": 20.0}
    assert got["Ann"]["earned"] == 0.0 and got["Ann"]["unpaid"] == 0.0 and got["Ann"]["orders"] == 1
    assert got["Zed"]["earned"] == 4.5 and got["Zed"]["unpaid"] == 0.0
    assert [c["customer"] for c in customers(rows)] == ["Sam", "Zed", "Ann"]  # biggest earner first
    assert customers([]) == []


def test_what_an_order_is_worth():
    assert order_value(OLD) == 20.0 and order_value({**OLD, "price_each": None}) is None
    assert order_value({**OLD, "price_each": 0.333, "quantity": 3}) == 1.0
