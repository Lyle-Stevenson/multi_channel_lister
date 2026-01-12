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
    return await ebay._get_user_access_token()


async def _get_offers_for_sku(ebay: EbayClient, *, sku: str, marketplace_id: str) -> list[dict[str, Any]]:
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
            print(f"[ERR] offer lookup failed sku={sku} HTTP {r.status_code}: {r.text}")
            return []
        data = r.json()
        offers = data.get("offers") or []
        return offers if isinstance(offers, list) else []


def _listing_id_from_offer_payload(offer: dict[str, Any]) -> str:
    listing = offer.get("listing") or {}
    lid = listing.get("listingId") or listing.get("listing_id") or ""
    return str(lid).strip() if lid else ""


async def _is_listing_active(ebay: EbayClient, *, listing_id: str) -> bool:
    """
    Uses Browse API getItem:
      GET /buy/browse/v1/item/{item_id}
    If it returns 200 -> listing is live/visible.
    If 404/4xx -> likely ended/not visible.
    """
    if not listing_id:
        return False

    url = f"{EBAY_API_BASE}/buy/browse/v1/item/{listing_id}"
    headers = {
        "Authorization": f"Bearer {await _token(ebay)}",
        "Accept": "application/json",
        # marketplace is important for browse; use EBAY_GB by default
        "X-EBAY-C-MARKETPLACE-ID": (settings.ebay_marketplace_id or "EBAY_GB"),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return True
        # Ended listings commonly 404 here, but keep it broad
        return False


async def _choose_active_offer_id(
    ebay: EbayClient,
    *,
    offer_ids: list[str],
) -> tuple[str | None, str | None]:
    """
    For each offerId:
      - getOffer(offerId) -> find listingId
      - verify listingId is active via Browse API
    Return (offerId, listingId) for the first active listing found.
    """
    for oid in offer_ids:
        try:
            offer = await ebay.get_offer(oid)
        except Exception as e:
            print(f"[WARN] getOffer failed offerId={oid}: {e!r}")
            continue

        listing_id = _listing_id_from_offer_payload(offer if isinstance(offer, dict) else {})
        if not listing_id:
            continue

        try:
            if await _is_listing_active(ebay, listing_id=listing_id):
                return oid, listing_id
        except Exception as e:
            print(f"[WARN] browse getItem failed listingId={listing_id}: {e!r}")
            continue

    return None, None


async def main() -> int:
    p = argparse.ArgumentParser(description="Fix ebay_offer_id + ebay_listing_id after end & relist, using SKU.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max", type=int, default=0, help="0 = no limit")
    p.add_argument("--sleep", type=float, default=0.15, help="Seconds to sleep between SKUs")
    args = p.parse_args()

    settings.validate_required()

    marketplace_id = (settings.ebay_marketplace_id or "EBAY_GB").strip() or "EBAY_GB"

    ebay = EbayClient(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        refresh_token=settings.ebay_refresh_token,
    )

    with SessionLocal() as db:
        q = db.query(ProductMap).order_by(ProductMap.updated_at.desc())
        if args.max and args.max > 0:
            q = q.limit(int(args.max))
        rows = q.all()

    print(f"Reconciling {len(rows)} rows (marketplace={marketplace_id}) dry_run={args.dry_run}")

    updated = 0
    skipped = 0
    missing = 0

    for pm in rows:
        sku = (pm.sku or "").strip()
        if not sku:
            skipped += 1
            continue

        offers = await _get_offers_for_sku(ebay, sku=sku, marketplace_id=marketplace_id)
        offer_ids = [str(o.get("offerId")).strip() for o in offers if isinstance(o, dict) and o.get("offerId")]
        offer_ids = [x for x in offer_ids if x]

        if not offer_ids:
            print(f"[MISS] sku={sku}: no offers found by SKU (unexpected if SKU exists on relisted items)")
            missing += 1
            await asyncio.sleep(args.sleep)
            continue

        # Choose offer whose listingId is still active
        new_offer_id, new_listing_id = await _choose_active_offer_id(ebay, offer_ids=offer_ids)

        if not new_offer_id:
            print(
                f"[MISS] sku={sku}: offers exist but none have an ACTIVE listingId. "
                f"(maybe visibility/ended or Browse scope missing)"
            )
            missing += 1
            await asyncio.sleep(args.sleep)
            continue

        changes = []
        if (pm.ebay_offer_id or "") != new_offer_id:
            changes.append(f"offer_id {pm.ebay_offer_id or '(none)'} -> {new_offer_id}")
        if new_listing_id and (pm.ebay_listing_id or "") != new_listing_id:
            changes.append(f"listing_id {pm.ebay_listing_id or '(none)'} -> {new_listing_id}")

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
                    row.ebay_offer_id = new_offer_id
                    if new_listing_id:
                        row.ebay_listing_id = new_listing_id
                    db.commit()

        updated += 1
        await asyncio.sleep(args.sleep)

    print(f"Done. updated={updated} skipped={skipped} missing={missing} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
