from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import xml.etree.ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory, ProductMap


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_any_text(root: ET.Element, wanted_local: str) -> Optional[str]:
    for el in root.iter():
        if _local(el.tag) == wanted_local and el.text and el.text.strip():
            return el.text.strip()
    return None


def _first_node(root: ET.Element, wanted_local: str) -> Optional[ET.Element]:
    for el in root.iter():
        if _local(el.tag) == wanted_local:
            return el
    return None


def _child_text(parent: ET.Element, wanted_local: str) -> Optional[str]:
    for c in list(parent):
        if _local(c.tag) == wanted_local and c.text and c.text.strip():
            return c.text.strip()
    return None


def _parse_ebay_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # "2026-01-03T22:32:24.943Z"
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@dataclass
class EbayPlatformEvent:
    event_name: str
    correlation_id: str
    item_id: str | None
    sku: str | None
    event_time: datetime | None

    # For ItemRevised
    quantity: int | None
    quantity_sold: int | None

    # For FixedPriceTransaction
    quantity_purchased: int | None


def parse_ebay_platform_notification(xml_bytes: bytes) -> EbayPlatformEvent:
    root = ET.fromstring(xml_bytes)

    event_name = _find_any_text(root, "NotificationEventName") or "Unknown"
    correlation_id = _find_any_text(root, "CorrelationID") or ""
    item_id = _find_any_text(root, "ItemID")
    sku = _find_any_text(root, "SKU")

    ts_txt = _find_any_text(root, "Timestamp")
    event_time = _parse_ebay_timestamp(ts_txt)

    # IMPORTANT: Quantity we want is Item/Quantity (direct child), not "any Quantity anywhere"
    item_el = _first_node(root, "Item")
    qty_txt = None
    qty_sold_txt = None

    if item_el is not None:
        qty_txt = _child_text(item_el, "Quantity")

        selling_status_el = None
        for c in list(item_el):
            if _local(c.tag) == "SellingStatus":
                selling_status_el = c
                break
        if selling_status_el is not None:
            qty_sold_txt = _child_text(selling_status_el, "QuantitySold")

    quantity = None
    quantity_sold = None
    if qty_txt is not None:
        try:
            quantity = int(float(qty_txt))
        except Exception:
            quantity = 0
    if qty_sold_txt is not None:
        try:
            quantity_sold = int(float(qty_sold_txt))
        except Exception:
            quantity_sold = 0

    # FixedPriceTransaction: sum QuantityPurchased occurrences
    purchased_total = 0
    found_purchase = False
    for el in root.iter():
        if _local(el.tag) == "QuantityPurchased" and el.text:
            try:
                purchased_total += int(float(el.text.strip()))
                found_purchase = True
            except Exception:
                continue
    quantity_purchased = purchased_total if found_purchase else None

    return EbayPlatformEvent(
        event_name=event_name.strip(),
        correlation_id=correlation_id.strip(),
        item_id=item_id.strip() if item_id else None,
        sku=sku.strip() if sku else None,
        event_time=event_time,
        quantity=quantity,
        quantity_sold=quantity_sold,
        quantity_purchased=quantity_purchased,
    )


def _lookup_product_map(db: Session, *, sku: str | None, item_id: str | None) -> ProductMap | None:
    if sku:
        pm = db.get(ProductMap, sku)
        if pm:
            return pm

    if item_id:
        return db.scalar(select(ProductMap).where(ProductMap.ebay_listing_id == str(item_id)))

    return None


async def apply_ebay_item_revised_and_sync_square(
    *, db: Session, event_id: str, pm: ProductMap, quantity: int, quantity_sold: int
) -> dict:
    """
    Treat eBay as source-of-truth: available = quantity - quantity_sold.
    """
    available = max(int(quantity) - int(quantity_sold), 0)

    inv = db.get(Inventory, pm.sku)
    if not inv:
        inv = Inventory(sku=pm.sku, on_hand=0)
        db.add(inv)

    before = int(inv.on_hand)
    inv.on_hand = available

    inv.last_source = "ebay"
    inv.last_source_at = utcnow()

    db.commit()
    return {"sku": pm.sku, "before": before, "after": available, "square_variation_id": pm.square_variation_id}


async def apply_ebay_fixed_price_txn_and_sync_square(*, db: Session, event_id: str, pm: ProductMap, qty_purchased: int) -> dict:
    inv = db.get(Inventory, pm.sku)
    if not inv:
        inv = Inventory(sku=pm.sku, on_hand=0)
        db.add(inv)

    before = int(inv.on_hand)
    after = max(before - int(qty_purchased), 0)
    inv.on_hand = after

    inv.last_source = "ebay"
    inv.last_source_at = utcnow()

    db.commit()
    return {"sku": pm.sku, "before": before, "after": after, "square_variation_id": pm.square_variation_id}
