# scripts/migrate_ebay_existing.py
from __future__ import annotations

import argparse
import html
import re
import tempfile
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import httpx
from sqlalchemy import text

from app.db import SessionLocal
from app.models import ProductMap, Inventory
from app.config import settings
from app.square_client import SquareClient
from app.square_service import SquareService
from app.ebay_client import EbayClient
from app.ebay_trading_client import EbayTradingClient

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_TAG_RE = re.compile(r"<[^>]+>")

def html_to_plain_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</\s*p\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*p(\s+[^>]*)?>", "", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()

def _next_sku(db) -> str:
    n = db.execute(text("SELECT nextval('sku_seq')")).scalar_one()
    return f"SKU{int(n):06d}"

def _ns_strip(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag

def parse_active_item_ids(trading_xml: str) -> list[str]:
    root = ET.fromstring(trading_xml)
    item_ids: list[str] = []
    for elem in root.iter():
        if _ns_strip(elem.tag) == "ItemID" and (elem.text or "").strip():
            item_ids.append(elem.text.strip())
    # This response contains ItemID in a few places; de-dupe while preserving order
    seen = set()
    out = []
    for x in item_ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def parse_item_details(get_item_xml: str) -> dict[str, Any]:
    root = ET.fromstring(get_item_xml)

    # Price extraction (fixed-price listings usually use StartPrice)
    start_price, start_cur = _find_money(root, "StartPrice")
    bin_price, bin_cur = _find_money(root, "BuyItNowPrice")
    cur_price, cur_cur = _find_money(root, "CurrentPrice")  # often under SellingStatus

    # Prefer BuyItNowPrice (if present), else StartPrice, else CurrentPrice.
    price = bin_price if bin_price is not None else (start_price if start_price is not None else cur_price)
    cur = bin_cur if bin_price is not None else (start_cur if start_price is not None else cur_cur)

    # If you call Trading with UK siteid (3), you should normally get GBP back.
    # If you ever get a different currency, you can fall back to ConvertedStartPrice/ConvertedBuyItNowPrice,
    # which eBay notes are calculated at request time. :contentReference[oaicite:1]{index=1}
    if price is None:
        price_gbp = None
    elif (cur or "").upper() == "GBP":
        price_gbp = float(price)
    else:
        # Try converted GBP prices (best-effort)
        conv_bin, conv_bin_cur = _find_money(root, "ConvertedBuyItNowPrice")
        conv_start, conv_start_cur = _find_money(root, "ConvertedStartPrice")
        conv = conv_bin if conv_bin is not None else conv_start
        conv_cur = conv_bin_cur if conv_bin is not None else conv_start_cur
        if conv is not None and (conv_cur or "").upper() == "GBP":
            price_gbp = float(conv)
        else:
            price_gbp = None

    def first_text(path_last: str) -> str | None:
        for elem in root.iter():
            if _ns_strip(elem.tag) == path_last:
                t = (elem.text or "").strip()
                if t:
                    return t
        return None

    item_id = first_text("ItemID")
    title = first_text("Title") or ""
    sku = first_text("SKU")  # may be empty
    desc = None
    # Description can be large / encoded; grab first Description element
    for elem in root.iter():
        if _ns_strip(elem.tag) == "Description":
            desc = elem.text or ""
            break

    qty = int(first_text("Quantity") or "0")
    qty_sold = int(first_text("QuantitySold") or "0")
    available = max(qty - qty_sold, 0)

    picture_urls: list[str] = []
    in_picture_details = False
    for elem in root.iter():
        if _ns_strip(elem.tag) == "PictureDetails":
            in_picture_details = True
        if in_picture_details and _ns_strip(elem.tag) == "PictureURL":
            u = (elem.text or "").strip()
            if u:
                picture_urls.append(u)

    return {
        "item_id": item_id,
        "title": title,
        "sku": sku,
        "price_gbp": price_gbp,
        "description_html": desc or "",
        "available_qty": available,
        "picture_urls": picture_urls,
    }

async def download_images(urls: list[str], tmp_dir: Path) -> list[Path]:
    paths: list[Path] = []
    async with httpx.AsyncClient(timeout=120) as client:
        for idx, u in enumerate(urls):
            r = await client.get(u)
            r.raise_for_status()
            # best-effort extension
            ext = ".jpg"
            lower = u.lower()
            for e in IMAGE_EXTS:
                if e in lower:
                    ext = e
                    break
            p = tmp_dir / f"ebay_{idx:02d}{ext}"
            p.write_bytes(r.content)
            paths.append(p)
    return paths

def _find_money(root: ET.Element, tag_name: str) -> tuple[float | None, str | None]:
    """
    Find first <tag_name currencyID="GBP">12.34</tag_name> anywhere and return (value, currencyID).
    Namespace-agnostic.
    """
    for el in root.iter():
        if _ns_strip(el.tag) == tag_name:
            txt = (el.text or "").strip()
            if not txt:
                continue
            try:
                val = float(txt)
            except Exception:
                continue
            cur = el.attrib.get("currencyID") or el.attrib.get("currencyId")
            return val, cur
    return None, None

async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max", type=int, default=0, help="0 = no limit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--assign-mode", choices=["assign_all", "assign_missing", "keep"], default="assign_all")
    args = p.parse_args()

    settings.validate_required()

    ebay_client = EbayClient(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        refresh_token=settings.ebay_refresh_token,
    )
    trading = EbayTradingClient(access_token_provider=ebay_client)

    square_client = SquareClient(access_token=settings.square_access_token, version=settings.square_version)
    square_service = SquareService(client=square_client, location_id=settings.square_location_id)

    # Fetch active listings (paginate)
    all_item_ids: list[str] = []
    page = 1
    while True:
        xml = await trading.get_my_ebay_selling_active(page=page, entries_per_page=100)
        ids = parse_active_item_ids(xml)
        # if a page returns nothing new, stop
        new = [i for i in ids if i not in set(all_item_ids)]
        if not new:
            break
        all_item_ids.extend(new)
        page += 1
        if args.max and len(all_item_ids) >= args.max:
            all_item_ids = all_item_ids[: args.max]
            break

    print(f"Found {len(all_item_ids)} active eBay listings")

    imported = 0
    for item_id in all_item_ids:
        with SessionLocal() as db:
            # Skip if already in DB by ebay_listing_id
            existing = db.query(ProductMap).filter(ProductMap.ebay_listing_id == str(item_id)).first()
            if existing:
                print(f"SKIP {item_id}: already mapped sku={existing.sku}")
                continue

        get_item_xml = await trading.get_item(item_id=str(item_id))
        d = parse_item_details(get_item_xml)
        title = d["title"]
        existing_sku = (d.get("sku") or "").strip()
        available_qty = int(d["available_qty"])
        picture_urls = d["picture_urls"]
        desc_html = d["description_html"]

        if not picture_urls:
            print(f"SKIP {item_id}: no pictures")
            continue

        with SessionLocal() as db:
            if args.assign_mode == "keep" and existing_sku:
                sku = existing_sku
            elif args.assign_mode == "assign_missing" and existing_sku:
                sku = existing_sku
            else:
                sku = _next_sku(db)

            print(f"IMPORT item={item_id} -> sku={sku} qty={available_qty} title={title[:60]!r}")

            if args.dry_run:
                imported += 1
                continue

            # 1) Set SKU on eBay listing if needed
            if (not existing_sku) or (existing_sku != sku):
                await trading.revise_item_set_sku(item_id=str(item_id), sku=sku)

            # 2) Migrate listing into Inventory API (to obtain offerId)
            mig = await ebay_client.bulk_migrate_listing([str(item_id)])
            # Expected shape: response per listing. We just hunt offerId.
            offer_id = None
            inventory_sku = sku

            for resp in (mig.get("responses") or []):
                if str(resp.get("listingId")) != str(item_id):
                    continue

                # Shape A: top-level offerId
                offer_id = resp.get("offerId") or (resp.get("offer") or {}).get("offerId")

                # Shape B: inventoryItems[] (what you're seeing)
                if not offer_id:
                    items = resp.get("inventoryItems") or []
                    if items and isinstance(items, list):
                        offer_id = items[0].get("offerId")

                break

            if not offer_id:
                raise RuntimeError(f"bulk_migrate_listing did not return offerId for listing {item_id}: {mig}")


            # 3) Download images to temp
            with tempfile.TemporaryDirectory() as td:
                tmp_dir = Path(td)
                img_paths = await download_images(picture_urls, tmp_dir)

                # 4) Create Square item (strip HTML for Square)
                price_gbp = d.get("price_gbp")
                if price_gbp is None:
                    print(f"SKIP {item_id}: could not determine GBP price from GetItem")
                    continue

                square_res = await square_service.upsert_item_with_images_and_inventory(
                    sku=sku,
                    name=title,
                    description=html_to_plain_text(desc_html),
                    price_gbp=float(price_gbp),
                    quantity=available_qty,
                    image_paths=img_paths,
                    reporting_category=None,
                )

            # 5) Insert into DB
            with SessionLocal() as db:
                pm = ProductMap(
                    sku=sku,
                    name=title,
                    square_item_id=square_res["square_item_id"],
                    square_variation_id=square_res["square_variation_id"],
                    ebay_inventory_sku=inventory_sku,
                    ebay_offer_id=str(offer_id),
                    ebay_listing_id=str(item_id),
                )
                db.add(pm)

                inv = Inventory(sku=sku, on_hand=available_qty, last_source="ebay")
                db.add(inv)
                db.commit()

            imported += 1

    print(f"Imported {imported} listings")
    return 0

if __name__ == "__main__":
    raise SystemExit(__import__("asyncio").run(main()))
