from __future__ import annotations

import argparse
import asyncio
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import ProductMap
from app.ebay_client import EbayClient

TRADING_ENDPOINT = "https://api.ebay.com/ws/api.dll"


def _ns_strip(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _first_text(elem: ET.Element, name: str) -> str | None:
    for e in elem.iter():
        if _ns_strip(e.tag) == name:
            t = (e.text or "").strip()
            if t:
                return t
    return None


def _iter_children(elem: ET.Element, name: str):
    for e in elem.iter():
        if _ns_strip(e.tag) == name:
            yield e


async def trading_get_active_sku_to_itemid(ebay: EbayClient, *, max_pages: int = 50) -> dict[str, str]:
    """
    Uses Trading GetMyeBaySelling to build {SKU -> active ItemID}.
    """
    token = await ebay._get_user_access_token()

    headers = {
        "Content-Type": "text/xml",
        "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
        "X-EBAY-API-SITEID": "3",  # UK
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1331",
        "X-EBAY-API-IAF-TOKEN": token,
    }

    sku_to_item: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=60) as client:
        for page in range(1, max_pages + 1):
            body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ActiveList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>200</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
  </ActiveList>
</GetMyeBaySellingRequest>
"""
            r = await client.post(TRADING_ENDPOINT, headers=headers, content=body.encode("utf-8"))
            if r.status_code >= 400:
                raise RuntimeError(f"Trading GetMyeBaySelling failed HTTP {r.status_code}: {r.text[:500]}")

            root = ET.fromstring(r.text)

            # items are under ...ActiveList/ItemArray/Item
            items = list(_iter_children(root, "Item"))
            if not items:
                break

            for item in items:
                item_id = _first_text(item, "ItemID")
                sku = _first_text(item, "SKU")
                if item_id and sku:
                    # If duplicates exist, prefer the most recent page hit (usually ok)
                    sku_to_item[sku.strip()] = item_id.strip()

    return sku_to_item


async def offers_for_sku(ebay: EbayClient, *, sku: str, marketplace_id: str) -> list[dict[str, Any]]:
    url = "https://api.ebay.com/sell/inventory/v1/offer"
    params = {"sku": sku, "marketplace_id": marketplace_id, "format": "FIXED_PRICE", "limit": "50"}
    token = await ebay._get_user_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers, params=params)
        if r.status_code >= 400:
            return []
        data = r.json()
        offers = data.get("offers") or []
        return offers if isinstance(offers, list) else []


def listing_id_from_offer(offer: dict[str, Any]) -> str | None:
    listing = offer.get("listing") or {}
    lid = listing.get("listingId") or listing.get("listing_id")
    return str(lid) if lid else None


async def migrate_listing_get_offer_id(ebay: EbayClient, *, listing_id: str, sku: str, marketplace_id: str) -> str | None:
    """
    Try bulk_migrate_listing to create the Inventory offer for this listing.
    If it already exists (409), fall back to scanning offers by SKU and matching listingId.
    """
    # bulk_migrate_listing should exist in your EbayClient
    try:
        mig = await ebay.bulk_migrate_listing([str(listing_id)])
        for resp in (mig.get("responses") or []):
            if str(resp.get("listingId")) != str(listing_id):
                continue
            items = resp.get("inventoryItems") or []
            if isinstance(items, list):
                for it in items:
                    if str(it.get("sku")) == str(sku) and it.get("offerId"):
                        return str(it["offerId"])
                if items and items[0].get("offerId"):
                    return str(items[0]["offerId"])
            if resp.get("offerId"):
                return str(resp["offerId"])
    except httpx.HTTPStatusError as e:
        # If your method uses raise_for_status, you may land here
        if "409" not in str(e):
            raise
    except Exception as e:
        # If it's 409 conflict in your implementation, it might show here too
        if "409" not in str(e):
            raise

    # fallback: find offer by SKU whose listingId matches this listing
    offers = await offers_for_sku(ebay, sku=sku, marketplace_id=marketplace_id)
    for o in offers:
        if listing_id_from_offer(o) == str(listing_id) and o.get("offerId"):
            return str(o["offerId"])

    return None


def update_row(db: Session, pm: ProductMap, *, new_listing_id: str, new_offer_id: str | None, dry_run: bool):
    changed = False

    if pm.ebay_listing_id != str(new_listing_id):
        changed = True
        if not dry_run:
            pm.ebay_listing_id = str(new_listing_id)

    if new_offer_id and pm.ebay_offer_id != str(new_offer_id):
        changed = True
        if not dry_run:
            pm.ebay_offer_id = str(new_offer_id)

    if changed and not dry_run:
        db.add(pm)
        db.commit()

    return changed


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max", type=int, default=0, help="0 = no limit")
    args = p.parse_args()

    settings.validate_required()

    ebay = EbayClient(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        refresh_token=settings.ebay_refresh_token,
    )

    marketplace_id = (settings.ebay_marketplace_id or "EBAY_GB").strip() or "EBAY_GB"

    print("Fetching ACTIVE listings from Trading (SKU -> ItemID)...")
    sku_to_active_item = await trading_get_active_sku_to_itemid(ebay)
    print(f"Trading returned {len(sku_to_active_item)} active SKU mappings")

    with SessionLocal() as db:
        q = db.query(ProductMap).order_by(ProductMap.sku.asc())
        rows: list[ProductMap] = q.all()

    if args.max and len(rows) > args.max:
        rows = rows[: args.max]

    updated = 0
    missing = 0
    skipped = 0

    for pm in rows:
        sku = pm.sku
        active_item_id = sku_to_active_item.get(sku)

        if not active_item_id:
            print(f"[MISS] sku={sku}: not found in Trading ActiveList")
            missing += 1
            continue

        # migrate that active listing into Inventory to get an offerId
        offer_id = await migrate_listing_get_offer_id(
            ebay, listing_id=active_item_id, sku=sku, marketplace_id=marketplace_id
        )

        if not offer_id:
            print(f"[MISS] sku={sku}: found active listingId={active_item_id} but could not obtain offerId")
            missing += 1
            continue

        with SessionLocal() as db:
            pm2 = db.query(ProductMap).filter(ProductMap.sku == sku).first()
            if not pm2:
                print(f"[MISS] sku={sku}: row disappeared")
                missing += 1
                continue

            changed = update_row(
                db,
                pm2,
                new_listing_id=active_item_id,
                new_offer_id=offer_id,
                dry_run=args.dry_run,
            )

        if changed:
            updated += 1
            print(f"[UPDATE] sku={sku}: listing_id={active_item_id} offer_id={offer_id} dry_run={args.dry_run}")
        else:
            skipped += 1
            print(f"[OK] sku={sku}: already correct (listing_id={active_item_id} offer_id={offer_id})")

    print(f"Done. updated={updated} skipped={skipped} missing={missing} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
