from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import xml.etree.ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory, ProductMap, WebhookEvent


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
    # naive but robust for webhook payload sizes
    def iter_children(e: ET.Element):
        for c in list(e):
            yield c

    # depth-first match sequence
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

    # return text of first final node with text
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

    # Common fields in many notification bodies (see eBay samples): NotificationEventName, CorrelationID, ItemID, SKU
    event_name = _find_any_text(root, "NotificationEventName") or "Unknown"
    correlation_id = _find_any_text(root, "CorrelationID") or ""

    # ItemID often exists under Item/ItemID for GetItemResponse, and also in GetItemTransactionsResponse
    item_id = _find_any_text(root, "ItemID")

    # SKU may be present in Item/SKU in some payloads
    sku = _find_any_text(root, "SKU")

    # ItemRevised comes as GetItemResponse-like body with Quantity + SellingStatus/QuantitySold
    qty_txt = _find_any_text(root, "Quantity")
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

    # FixedPriceTransaction uses GetItemTransactions(ReturnAll) payload :contentReference[oaicite:3]{index=3}
    # QuantityPurchased is under Transaction/QuantityPurchased (can be multiple transactions; we will sum)
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


async def apply_ebay_item_revised_and_sync_square(*, db: Session, event_id: str, pm: ProductMap, quantity: int, quantity_sold: int) -> dict:
    """
    Treat eBay manual edit as source-of-truth: available = Quantity - QuantitySold.
    eBay returns QuantitySold in the SellingStatus for many notification bodies (GetItemResponse-style). :contentReference[oaicite:4]{index=4}
    """
    available = max(int(quantity) - int(quantity_sold), 0)

    inv = db.get(Inventory, pm.sku)
    if not inv:
        inv = Inventory(sku=pm.sku, on_hand=0)
        db.add(inv)

    before = int(inv.on_hand)
    inv.on_hand = available
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
    db.commit()

    return {"sku": pm.sku, "before": before, "after": after, "square_variation_id": pm.square_variation_id}
