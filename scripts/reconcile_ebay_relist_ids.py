from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from app.config import settings
from app.db import SessionLocal
from app.models import ProductMap

EBAY_API_BASE = "https://api.ebay.com"


def _ns_strip(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _first_text(root: ET.Element, tag_last: str) -> str | None:
    for el in root.iter():
        if _ns_strip(el.tag) == tag_last:
            t = (el.text or "").strip()
            if t:
                return t
    return None


def _parse_active_item_ids(trading_xml: str) -> list[str]:
    root = ET.fromstring(trading_xml)
    ids: list[str] = []
    for el in root.iter():
        if _ns_strip(el.tag) == "ItemID" and (el.text or "").strip():
            ids.append(el.text.strip())
    # de-dupe preserving order
    seen = set()
    out = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _parse_get_item_sku(get_item_xml: str) -> tuple[str | None, str | None]:
    root = ET.fromstring(get_item_xml)
    item_id = _first_text(root, "ItemID")
    sku = _first_text(root, "SKU")
    return item_id, sku


async def _offer_id_from_sku(ebay_client: Any, sku: str) -> str | None:
    """
    Uses Sell Inventory API:
      GET /sell/inventory/v1/offer?sku={sku}
    Returns first offerId if present.
    """
    token = await ebay_client.get_access_token()
    url = f"{EBAY_API_BASE}/sell/inventory/v1/offer?sku={sku}"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            return None
        data = r.json()
        offers = data.get("offers") or []
        if not offers:
            return None
        oid = offers[0].get("offerId")
        return str(oid) if oid else None


async def main() -> int:
    p = argparse.ArgumentParser(description="Reconcile product_map.ebay_listing_id after eBay relist, using SKU.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max", type=int, default=0, help="0 = no limit")
    p.add_argument("--update-offer-id", action="store_true", help="Also refresh ebay_offer_id via offer lookup by SKU.")
    args = p.parse_args()

    settings.validate_required()

    # Imports here so the script stays minimal
    from app.ebay_client import EbayClient
    from app.ebay_trading_client import EbayTradingClient

    ebay_client = EbayClient(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        refresh_token=settings.ebay_refresh_token,
    )
    trading = EbayTradingClient(access_token_provider=ebay_client)

    # Build map of sku -> current active item_id
    sku_to_item_id: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}

    page = 1
    collected = 0
    while True:
        xml = await trading.get_my_ebay_selling_active(page=page, entries_per_page=100)
        item_ids = _parse_active_item_ids(xml)
        if not item_ids:
            break

        for item_id in item_ids:
            get_item_xml = await trading.get_item(item_id=str(item_id))
            parsed_item_id, sku = _parse_get_item_sku(get_item_xml)
            if not parsed_item_id:
                continue
            sku = (sku or "").strip()
            if not sku:
                continue

            if sku in sku_to_item_id and sku_to_item_id[sku] != parsed_item_id:
                duplicates.setdefault(sku, []).append(parsed_item_id)
            else:
                sku_to_item_id[sku] = parsed_item_id

            collected += 1
            if args.max and collected >= args.max:
                break

        if args.max and collected >= args.max:
            break

        page += 1

    print(f"Active listings scanned: {collected}")
    print(f"Active SKU->ItemID mappings found: {len(sku_to_item_id)}")
    if duplicates:
        print("WARNING: duplicate SKUs found on active listings (first one wins):")
        for sku, ids in list(duplicates.items())[:20]:
            print(f"  {sku}: {ids}")

    # Now reconcile DB
    changes = 0
    missing_in_ebay = 0
    missing_in_db = 0

    with SessionLocal() as db:
        rows = db.query(ProductMap).all()

        for pm in rows:
            sku = (pm.sku or "").strip()
            if not sku:
                continue

            new_item_id = sku_to_item_id.get(sku)
            if not new_item_id:
                missing_in_ebay += 1
                continue

            old_item_id = (pm.ebay_listing_id or "").strip()
            needs_listing_update = (old_item_id != str(new_item_id))

            new_offer_id: str | None = None
            needs_offer_update = False
            if args.update_offer_id:
                new_offer_id = await _offer_id_from_sku(ebay_client, sku)
                if new_offer_id and (str(pm.ebay_offer_id or "").strip() != str(new_offer_id)):
                    needs_offer_update = True

            if needs_listing_update or needs_offer_update:
                changes += 1
                print(
                    f"SKU {sku}: listing_id {old_item_id or '(none)'} -> {new_item_id}"
                    + (f" | offer_id {pm.ebay_offer_id or '(none)'} -> {new_offer_id}" if needs_offer_update else "")
                )

                if not args.dry_run:
                    pm.ebay_listing_id = str(new_item_id)
                    # These are typically the same for you (single variation)
                    if pm.ebay_inventory_sku:
                        pm.ebay_inventory_sku = sku
                    if needs_offer_update and new_offer_id:
                        pm.ebay_offer_id = str(new_offer_id)

        if not args.dry_run:
            db.commit()

    print(f"DB rows changed: {changes}{' (dry-run only)' if args.dry_run else ''}")
    print(f"DB SKUs missing in active eBay listings: {missing_in_ebay}")
    print(f"Done.")
    return 0


if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
