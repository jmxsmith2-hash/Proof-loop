"""Order intake: turn shop orders into queued plates.

Shopify can POST its ``orders/create`` webhook straight to ClearLoop. Etsy,
WooCommerce, eBay or anything else can use the generic endpoint, typically via
n8n, Zapier or a short script.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
from pathlib import Path


def verify_shopify(raw_body: bytes, header_hmac: str | None, secret: str | None) -> bool:
    if not secret or not header_hmac:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, header_hmac.strip())


def shopify_lines(order: dict) -> list[tuple[str, int]]:
    out = []
    for item in order.get("line_items", []) or []:
        sku = (item.get("sku") or "").strip()
        qty = int(item.get("quantity") or 0)
        if sku and qty > 0:
            out.append((sku, qty))
    return out


def plates_needed(quantity: int, parts_per_plate: int) -> int:
    return math.ceil(quantity / max(1, parts_per_plate))


def enqueue_order(farm, lines: list[tuple[str, int]], order_ref: str, base_dir: Path | None = None) -> dict:
    skus = {s.sku: s for s in farm.cfg.skus}
    queued, unknown = [], []
    for sku, qty in lines:
        s = skus.get(sku)
        if s is None:
            unknown.append(sku)
            continue
        path = Path(s.file)
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        ids = farm.enqueue_file(
            path,
            name=f"{sku} ({order_ref})",
            copies=plates_needed(qty, s.parts_per_plate),
            models=s.models,
            order_ref=order_ref,
            sku=sku,
        )
        queued.append({"sku": sku, "quantity": qty, "plates": len(ids), "jobs": ids})
    if unknown:
        farm.store.add_event(
            None,
            "warning",
            "order_unknown_sku",
            f"order {order_ref}: no file mapped for SKU(s) {', '.join(unknown)}",
        )
    return {"order": order_ref, "queued": queued, "unknown_skus": unknown}
