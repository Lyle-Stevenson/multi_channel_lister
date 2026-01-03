from __future__ import annotations

import base64
import hashlib
import hmac
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Inventory, ProductMap, WebhookEvent

SQUARE_BASE = "https://connect.squareup.com/v2"
EBAY_BASE = "https://api.ebay.com"

# Simple in-process cache to avoid refreshing eBay token on every webhook burst
_ebay_cached_token: dict[str, Any] | None = None


def verify_square_signature(*, raw_body: bytes, signature: str | None) -> bool:
    """
    Square: base64(HMAC_SHA256(signature_key, notification_url + request_body))
    """
    if not signature:
        return False
    if not settings.square_webhook_signature_key or not settings.square_webhook_notification_url:
        return False

    message = (settings.square_webhook_notification_url + raw_body.decode("utf-8")).encode("utf-8")
    digest = hmac.new(settings.square_webhook_signature_key.encode("utf-8"), message, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _safe_get(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def extract_payment_order_id_and_status(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Handles Square payment.* webhooks.
    """
    payment = _safe_get(payload, "data", "object", "payment") or {}
    if not isinstance(payment, dict):
        return (None, None)
    order_id = payment.get("order_id") or payment.get("orderId")
    status = payment.get("status")
    return (str(order_id) if order_id else None, str(status) if status else None)


def extract_inventory_change(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Try to normalize Square inventory change event payloads.

    Returns a list of changes like:
      [{"catalog_object_id": "VARIATION_ID", "quantity": 12, "state": "IN_STOCK"}]

    Square payload shapes vary; this function is defensive.
    """
    obj = _safe_get(payload, "data", "object") or {}
    if not isinstance(obj, dict):
        return []

    # Some events may have "inventory_counts" list
    counts = obj.get("inventory_counts") or obj.get("inventoryCounts")
    if isinstance(counts, list):
        out: list[dict[str, Any]] = []
        for c in counts:
            if not isinstance(c, dict):
                continue
            cat_id = c.get("catalog_object_id") or c.get("catalogObjectId")
            qty_raw = c.get("quantity") or c.get("calculated_at")  # fallback not great; keep defensive
            state = c.get("state") or ""
            # quantity is usually a string number like "3"
            try:
                qty = int(float(c.get("quantity") or "0"))
            except Exception:
                qty = 0
            if cat_id:
                out.append({"catalog_object_id": str(cat_id), "quantity": qty, "state": str(state)})
        return out

    # Some events may have single "inventory_count"
    count = obj.get("inventory_count") or obj.get("inventoryCount")
    if isinstance(count, dict):
        cat_id = count.get("catalog_object_id") or count.get("catalogObjectId")
        state = count.get("state") or ""
        try:
            qty = int(float(count.get("quantity") or "0"))
        except Exception:
            qty = 0
        if cat_id:
            return [{"catalog_object_id": str(cat_id), "quantity": qty, "state": str(state)}]

    return []


async def _square_retrieve_order(order_id: str) -> dict[str, Any]:
    url = f"{SQUARE_BASE}/orders/{order_id}"
    headers = {
        "Authorization": f"Bearer {settings.square_access_token}",
        "Square-Version": settings.square_version,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Square retrieve order failed: HTTP {r.status_code}: {r.text}")
        return r.json()


def _ebay_basic_auth_header() -> str:
    raw = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


async def _ebay_get_access_token() -> str:
    global _ebay_cached_token

    now = datetime.now(timezone.utc)
    if _ebay_cached_token and _ebay_cached_token["expires_at"] > now + timedelta(minutes=2):
        return _ebay_cached_token["access_token"]

    url = f"{EBAY_BASE}/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _ebay_basic_auth_header(),
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": settings.ebay_refresh_token,
        "scope": " ".join(
            [
                "https://api.ebay.com/oauth/api_scope/sell.inventory",
                "https://api.ebay.com/oauth/api_scope/sell.account",
            ]
        ),
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, data=data)
        if r.status_code >= 400:
            raise RuntimeError(f"eBay token refresh failed: HTTP {r.status_code}: {r.text}")
        payload = r.json()

    access_token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 7200))
    _ebay_cached_token = {"access_token": access_token, "expires_at": now + timedelta(seconds=expires_in)}
    return access_token


async def _ebay_bulk_update_quantity(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"responses": []}

    url = f"{EBAY_BASE}/sell/inventory/v1/bulk_update_price_quantity"
    token = await _ebay_get_access_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-GB",
    }

    payload = {
        "requests": [
            {
                "sku": it["sku"],
                "shipToLocationAvailability": {"quantity": int(it["qty"])},
                "offers": [{"offerId": it["offer_id"], "availableQuantity": int(it["qty"])}],
            }
            for it in items
        ]
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"eBay bulk_update_price_quantity failed: HTTP {r.status_code}: {r.text}")
        return r.json()


def _sync_all_ebay_offers_from_db(*, db: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pm in db.execute(select(ProductMap)).scalars().all():
        if not pm.ebay_offer_id:
            continue
        inv = db.get(Inventory, pm.sku)
        qty = int(inv.on_hand) if inv else 0
        rows.append({"sku": pm.sku, "offer_id": str(pm.ebay_offer_id), "qty": qty})
    return rows


async def apply_square_order_and_sync_ebay(
    *,
    db: Session,
    event_id: str,
    event_type: str,
    order_id: str,
) -> dict[str, Any]:
    event = db.get(WebhookEvent, event_id)
    if not event:
        event = WebhookEvent(event_id=event_id, provider="square", event_type=event_type, order_id=order_id)
        db.add(event)
        db.commit()

    if event.applied_inventory:
        if not event.ebay_synced:
            offer_map = _sync_all_ebay_offers_from_db(db=db)
            await _ebay_bulk_update_quantity(offer_map)
            event.ebay_synced = True
            db.commit()
        return {"event_id": event_id, "order_id": order_id, "applied_inventory": True, "ebay_synced": event.ebay_synced}

    order_payload = await _square_retrieve_order(order_id)
    order = order_payload.get("order") or {}
    line_items = order.get("line_items") or []

    sold_by_variation: dict[str, int] = defaultdict(int)
    for li in line_items:
        var_id = li.get("catalog_object_id") or ""
        qty_raw = li.get("quantity") or "0"
        try:
            qty = int(float(qty_raw))
        except Exception:
            qty = 0
        if var_id and qty > 0:
            sold_by_variation[var_id] += qty

    decremented: list[dict[str, Any]] = []
    offer_id_to_qty: dict[str, int] = {}

    for var_id, qty_sold in sold_by_variation.items():
        pm = db.scalar(select(ProductMap).where(ProductMap.square_variation_id == var_id))
        if not pm:
            continue

        inv = db.get(Inventory, pm.sku)
        if not inv:
            inv = Inventory(sku=pm.sku, on_hand=0)
            db.add(inv)

        before = int(inv.on_hand)
        after = max(before - int(qty_sold), 0)
        inv.on_hand = after

        decremented.append({"sku": pm.sku, "sold": qty_sold, "before": before, "after": after})

        if pm.ebay_offer_id:
            offer_id_to_qty[str(pm.ebay_offer_id)] = after

    event.applied_inventory = True
    db.commit()

    ebay_synced = True
    if offer_id_to_qty:
        try:
            await _ebay_bulk_update_quantity(offer_id_to_qty)
        except Exception as e:
            print("EBAY SYNC FAILED:", repr(e))
            ebay_synced = False

    event.ebay_synced = ebay_synced
    db.commit()

    return {"event_id": event_id, "order_id": order_id, "applied_inventory": True, "ebay_synced": ebay_synced, "decremented": decremented}


async def apply_square_inventory_change_and_sync_ebay(
    *,
    db: Session,
    event_id: str,
    event_type: str,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    When Square inventory is manually changed, we treat Square as source-of-truth and set DB on_hand to
    the IN_STOCK quantity, then push that to eBay.
    """
    event = db.get(WebhookEvent, event_id)
    if not event:
        event = WebhookEvent(event_id=event_id, provider="square", event_type=event_type, order_id=None)
        db.add(event)
        db.commit()

    if event.applied_inventory:
        if not event.ebay_synced:
            offer_map = _sync_all_ebay_offers_from_db(db=db)
            await _ebay_bulk_update_quantity(offer_map)
            event.ebay_synced = True
            db.commit()
        return {"event_id": event_id, "applied_inventory": True, "ebay_synced": event.ebay_synced}

    updated: list[dict[str, Any]] = []
    offer_id_to_qty: dict[str, int] = {}

    for ch in changes:
        var_id = str(ch.get("catalog_object_id") or "")
        qty = int(ch.get("quantity") or 0)
        state = str(ch.get("state") or "")

        # Only sync IN_STOCK counts
        if state and state != "IN_STOCK":
            continue
        if not var_id:
            continue

        pm = db.scalar(select(ProductMap).where(ProductMap.square_variation_id == var_id))
        if not pm:
            continue

        inv = db.get(Inventory, pm.sku)
        if not inv:
            inv = Inventory(sku=pm.sku, on_hand=0)
            db.add(inv)

        before = int(inv.on_hand)
        inv.on_hand = max(qty, 0)
        after = int(inv.on_hand)

        updated.append({"sku": pm.sku, "before": before, "after": after, "square_variation_id": var_id})

        if pm.ebay_offer_id:
            offer_id_to_qty[str(pm.ebay_offer_id)] = after

    event.applied_inventory = True
    db.commit()

    ebay_synced = True
    if offer_id_to_qty:
        try:
            await _ebay_bulk_update_quantity(offer_id_to_qty)
        except Exception as e:
            print("EBAY SYNC FAILED:", repr(e))
            ebay_synced = False
    event.ebay_synced = ebay_synced
    db.commit()

    return {"event_id": event_id, "applied_inventory": True, "ebay_synced": ebay_synced, "updated": updated}
