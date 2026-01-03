from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import xml.etree.ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory, ProductMap


def _local(tag: str) -> str:
    # "{namespace}Tag" -> "Tag"
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_first_text(root: ET.Element, path_locals: list[str]) -> Optional[str]:
    """
    Find first matching element by walking all descendants and matching localnames in order.
    This is namespace-agnostic and works across eBay SOAP variants.
    """

    nodes = [root]
    for wanted in path_locals:
        nxt: list[ET.Element] = []
        for n in nodes:
            for d in n.iter():
                if _local(d.tag) == wanted:
                    nxt.append(d)
        if not nxt:
            return None
        nodes = nxt

    for n in nodes:
        if n.text and n.text.strip():
            return n.text.strip()
    return None


def _find_any_text(root: ET.Element, wanted_local: str) -> Optional[str]:
    for el in root.iter():
        if _local(el.tag) == wanted_local and el.text and el.text.strip():
            return el.text.strip()
    return None


@dataclass
class EbayPlatformEvent:
    event_name: str
    correlation_id: str
    item_id: str | None
    sku: str | None

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

    # Prefer Item-scoped fields to avoid picking up unrelated <Quantity> elsewhere in the SOAP.
    item_el = None
    for el in root.iter():
        if _local(el.tag) == "Item":
            item_el = el
            break

    qty_txt = None
    qty_sold_txt = None

    if item_el is not None:
        # Quantity directly under Item
        for el in item_el.iter():
            if _local(el.tag) == "Quantity" and el.text and el.text.strip():
                qty_txt = el.text.strip()
                break

        # QuantitySold under Item/SellingStatus
        selling_status_el = None
        for el in item_el.iter():
            if _local(el.tag) == "SellingStatus":
                selling_status_el = el
                break
        if selling_status_el is not None:
            for el in selling_status_el.iter():
                if _local(el.tag) == "QuantitySold" and el.text and el.text.strip():
                    qty_sold_txt = el.text.strip()
                    break

    # Fallbacks if Item-scoped values missing
    if qty_txt is None:
        qty_txt = _find_any_text(root, "Quantity")
    if qty_sold_txt is None:
        qty_sold_txt = _find_any_text(root, "QuantitySold")


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

    # FixedPriceTransaction can include multiple QuantityPurchased entries; sum them
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
        quantity=quantity,
        quantity_sold=quantity_sold,
        quantity_purchased=quantity_purchased,
    )


def _lookup_product_map(db: Session, *, sku: str | None, item_id: str | None) -> ProductMap | None:
    # Prefer direct sku match (your primary key)
    if sku:
        pm = db.get(ProductMap, sku)
        if pm:
            return pm

    # Fallback: match by ebay_listing_id
    if item_id:
        return db.scalar(select(ProductMap).where(ProductMap.ebay_listing_id == str(item_id)))

    return None


async def apply_ebay_item_revised_and_sync_square(
    *,
    db: Session,
    event_id: str,
    pm: ProductMap,
    quantity: int,
    quantity_sold: int,
) -> dict:
    """
    Treat eBay manual edit as source-of-truth: available = Quantity - QuantitySold.
    """
    available = max(int(quantity) - int(quantity_sold), 0)

    inv = db.get(Inventory, pm.sku)
    if not inv:
        inv = Inventory(sku=pm.sku, on_hand=0)
        db.add(inv)

    before = int(inv.on_hand)

    # No-op guard
    if before == available:
        # Still mark that this was an ebay-origin confirmation (helps clear Square echo later)
        inv.last_source = "ebay"
        inv.last_source_at = datetime.now(timezone.utc)
        db.commit()
        return {"sku": pm.sku, "before": before, "after": available, "square_variation_id": pm.square_variation_id}

    inv.on_hand = available
    inv.last_source = "ebay"
    inv.last_source_at = datetime.now(timezone.utc)
    db.commit()

    return {"sku": pm.sku, "before": before, "after": available, "square_variation_id": pm.square_variation_id}


async def apply_ebay_fixed_price_txn_and_sync_square(
    *,
    db: Session,
    event_id: str,
    pm: ProductMap,
    qty_purchased: int,
) -> dict:
    inv = db.get(Inventory, pm.sku)
    if not inv:
        inv = Inventory(sku=pm.sku, on_hand=0)
        db.add(inv)

    before = int(inv.on_hand)
    after = max(before - int(qty_purchased), 0)

    # No-op guard
    if before == after:
        inv.last_source = "ebay"
        inv.last_source_at = datetime.now(timezone.utc)
        db.commit()
        return {"sku": pm.sku, "before": before, "after": after, "square_variation_id": pm.square_variation_id}

    inv.on_hand = after
    inv.last_source = "ebay"
    inv.last_source_at = datetime.now(timezone.utc)
    db.commit()

    return {"sku": pm.sku, "before": before, "after": after, "square_variation_id": pm.square_variation_id}
