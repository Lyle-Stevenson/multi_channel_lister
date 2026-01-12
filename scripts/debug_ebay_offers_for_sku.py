from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx

from app.config import settings
from app.ebay_client import EbayClient

EBAY_API_BASE = "https://api.ebay.com"


def _listing_id_from_offer(offer: dict[str, Any]) -> str:
    listing = offer.get("listing") or {}
    lid = listing.get("listingId") or listing.get("listing_id") or ""
    return str(lid).strip() if lid else ""


async def _get_offers_for_sku(ebay: EbayClient, *, sku: str, marketplace_id: str) -> list[dict[str, Any]]:
    url = f"{EBAY_API_BASE}/sell/inventory/v1/offer"
    params = {
        "sku": sku,
        "marketplace_id": marketplace_id,
        "format": "FIXED_PRICE",
        "limit": "50",
    }
    token = await ebay._get_user_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers, params=params)
        print("offer?sku= HTTP", r.status_code)
        if r.status_code >= 400:
            print(r.text)
            return []
        data = r.json()
        offers = data.get("offers") or []
        return offers if isinstance(offers, list) else []


async def test_revisable(ebay: EbayClient, *, sku: str, offer_id: str, qty: int) -> tuple[bool, str]:
    """
    Calls bulk_update_price_quantity with a no-op quantity.
    If offer is tied to an ended listing, eBay returns errorId 25002.
    """
    try:
        res = await ebay.bulk_update_price_quantity(
            sku=sku,
            offer_id=offer_id,
            merchant_location_key=settings.ebay_merchant_location_key,
            quantity=int(qty),
        )
    except Exception as e:
        return False, f"exception: {e!r}"

    responses = res.get("responses") or []
    for r in responses:
        # some responses omit offerId on the first error object, so be permissive
        if r.get("offerId") and str(r.get("offerId")) != str(offer_id):
            continue

        status = int(r.get("statusCode") or 0)
        if status and status < 400:
            return True, "ok"

        errs = r.get("errors") or []
        for err in errs:
            if int(err.get("errorId") or 0) == 25002:
                # ended item
                item_id = None
                for p in err.get("parameters") or []:
                    if p.get("name") == "ItemID":
                        item_id = p.get("value")
                return False, f"ended-item(25002) ItemID={item_id}"
        return False, f"statusCode={status} errors={errs}"

    return False, "no matching response in bulk_update result"


async def main() -> int:
    p = argparse.ArgumentParser(description="Debug offers returned by SKU and test which one is revisable (active).")
    p.add_argument("--sku", required=True)
    p.add_argument("--marketplace", default=None)
    p.add_argument("--qty", type=int, default=0, help="No-op quantity to use for test (default 0)")
    p.add_argument("--raw", action="store_true", help="Print raw offer JSON too")
    args = p.parse_args()

    settings.validate_required()
    marketplace_id = (args.marketplace or settings.ebay_marketplace_id or "EBAY_GB").strip() or "EBAY_GB"

    ebay = EbayClient(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        refresh_token=settings.ebay_refresh_token,
    )

    sku = args.sku.strip()
    offers = await _get_offers_for_sku(ebay, sku=sku, marketplace_id=marketplace_id)
    print(f"offers returned: {len(offers)} marketplace={marketplace_id}")

    for i, o in enumerate(offers, start=1):
        if not isinstance(o, dict):
            continue
        offer_id = o.get("offerId")
        status = o.get("status")
        listing_id = _listing_id_from_offer(o)

        print(f"\n--- Offer #{i} ---")
        print("offerId   =", offer_id)
        print("status    =", status)
        print("listingId =", listing_id or "(none)")

        if offer_id:
            ok, why = await test_revisable(ebay, sku=sku, offer_id=str(offer_id), qty=int(args.qty))
            print("revisable =", ok, f"({why})")

        if args.raw:
            print("RAW:")
            print(json.dumps(o, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
