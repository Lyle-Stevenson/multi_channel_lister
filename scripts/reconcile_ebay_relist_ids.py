# scripts/reconcile_ebay_relist_ids.py
from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx

from app.config import settings
from app.db import SessionLocal
from app.models import ProductMap
from app.ebay_client import EbayClient

EBAY_API_BASE = "https://api.ebay.com"


async def _token(ebay: EbayClient) -> str:
    # Your EbayClient exposes _get_user_access_token(), not get_access_token()
    return await ebay._get_user_access_token()


async def _get_offers_for_sku(ebay: EbayClient, *, sku: str, marketplace_id: str) -> list[dict[str, Any]]:
    """
    Calls: GET /sell/inventory/v1/offer?sku=...&marketplace_id=...&format=FIXED_PRICE
    Returns list of offers.
    """
    url = f"{EBAY_API_BASE}/sell/inventory/v1/offer"
    params = {
        "sku": sku,
        "marketplace_id": marketplace_id,
        "format": "FIXED_PRICE",
        "limit": "50",
    }
    headers = {"Authorization": f"Bearer {await _token(ebay)}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers, params=params)
        if r.status_code >= 400:
            # Helpful debug
            print(f"[ERR] offer lookup failed sku={sku} HTTP {r.status_code}: {r.text}")
            return []
        data = r.json()
        offers = data.get("offers") or []
        return offers if isinstance(offers, list) else []


def _pick_best_offer(offers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Prefer offers that already have listing info (published).
    """
    if not offers:
        return None
    for o in offers:
        if isinstance(o, dict) and o.get("offerId") and o.get("listing"):
            return o
    for o in offers:
        if isinstance(o, dict) and o.get("offerId"):
            return o
    return offers[0] if isinstance(offers[0], dict) else None


def _extract_listing_id(offer: dict[str, Any]) -> str:
    listing = offer.get("listing") or {}
    lid = listing.get("listingId") or listing.get("listing_id") or ""
    return str(lid).strip() if lid else ""


async def main() -> int:
    p = argparse.ArgumentParser(description="Fix product_map ebay ids after relist, using SKU -> Offer -> listingId.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max", type=int, default=0, help="0 = no limit")
    p.add_argument("--marketplace", default=None, help="Defaults to settings.ebay_marketplace_id (usually EBAY_GB)")
    p.add_argument("--sleep", type=float, default=0.15, help="Seconds to sleep between SKUs")
    args = p.parse_args()

    settings.validate_required()

    marketplace_id = (args.marketplace or settings.ebay_marketplace_id or "EBAY_GB").strip() or "EBAY_GB"

    ebay = EbayClient(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        refresh_token=settings.ebay_refresh_token,
    )

    # Read rows once
    with SessionLocal() as db:
        q = db.query(ProductMap).order_by(ProductMap.updated_at.desc())
        if args.max and args.max > 0:
            q = q.limit(int(args.max))
        rows = q.all()

    print(f"Reconciling {len(rows)} product_map rows (marketplace={marketplace_id}) dry_run={args.dry_run}")

    updated = 0
    skipped = 0
    missing = 0

    for pm in rows:
        sku = (pm.sku or "").strip()
        if not sku:
            skipped += 1
            continue

        offers = await _get_offers_for_sku(ebay, sku=sku, marketplace_id=marketplace_id)
        best = _pick_best_offer(offers)
        if not best:
            print(f"[MISS] sku={sku}: no offer found by SKU (likely SKU missing on eBay relist)")
            missing += 1
            await asyncio.sleep(args.sleep)
            continue

        offer_id = str(best.get("offerId") or "").strip()
        if not offer_id:
            print(f"[MISS] sku={sku}: offer record missing offerId??")
            missing += 1
            await asyncio.sleep(args.sleep)
            continue

        # Fetch full offer to get listingId reliably
        offer = await ebay.get_offer(offer_id)
        listing_id = _extract_listing_id(offer)

        changes = []
        if offer_id and (pm.ebay_offer_id or "") != offer_id:
            changes.append(f"offer_id {pm.ebay_offer_id or '(none)'} -> {offer_id}")
        if listing_id and (pm.ebay_listing_id or "") != listing_id:
            changes.append(f"listing_id {pm.ebay_listing_id or '(none)'} -> {listing_id}")

        if not changes:
            print(f"[OK]  sku={sku}: no change (offer_id={pm.ebay_offer_id}, listing_id={pm.ebay_listing_id})")
            skipped += 1
            await asyncio.sleep(args.sleep)
            continue

        print(f"[UPD] sku={sku}: " + "; ".join(changes))

        if not args.dry_run:
            with SessionLocal() as db:
                row = db.get(ProductMap, sku)
                if row:
                    row.ebay_offer_id = offer_id
                    # Only overwrite listing_id if we actually got one
                    if listing_id:
                        row.ebay_listing_id = listing_id
                    db.commit()

        updated += 1
        await asyncio.sleep(args.sleep)

    print(f"Done. updated={updated} skipped={skipped} missing={missing} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
