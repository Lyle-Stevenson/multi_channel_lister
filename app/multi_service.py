from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory, ProductMap
from app.square_service import SquareService
from app.ebay_service import EbayService


class MultiChannelService:
    def __init__(self, square: SquareService, ebay: EbayService):
        self.square = square
        self.ebay = ebay

    def _get_or_create_inventory(self, db: Session, sku: str) -> Inventory:
        inv = db.scalar(select(Inventory).where(Inventory.sku == sku))
        if inv:
            return inv
        inv = Inventory(sku=sku, on_hand=0)
        db.add(inv)
        db.flush()
        return inv

    async def upsert_both(
        self,
        *,
        db: Session,
        sku: str,
        square_title: str,
        ebay_title: str,
        price_gbp: float,
        quantity: int,
        description_html: str,
        image_paths: list[Path],
        # Square
        square_reporting_category: str | None,
        # eBay
        ebay_category_id: str,
        ebay_condition: str,
        ebay_item_specifics: dict[str, str] | None,
    ) -> dict[str, Any]:
        # 1) Shared inventory source of truth
        inv = self._get_or_create_inventory(db, sku)
        inv.on_hand = int(quantity)

        # 2) Product mapping row
        pm = db.scalar(select(ProductMap).where(ProductMap.sku == sku))
        if not pm:
            pm = ProductMap(sku=sku, name=ebay_title)
            db.add(pm)
            db.flush()
        else:
            pm.name = ebay_title

        # 3) Push to Square first
        square_res = await self.square.upsert_item_with_images_and_inventory(
            sku=sku,
            name=square_title,
            description=description_html,
            price_gbp=price_gbp,
            quantity=inv.on_hand,
            image_paths=image_paths,
            reporting_category=square_reporting_category,
        )

        pm.square_item_id = square_res.get("square_item_id")
        pm.square_variation_id = square_res.get("square_variation_id")

        # 4) Push to eBay next (use same DB qty)
        ebay_res = await self.ebay.upsert_listing_with_images_and_inventory(
            sku=sku,
            title=ebay_title,
            description=description_html,
            category_id=ebay_category_id,
            condition=ebay_condition,
            price_gbp=price_gbp,
            quantity=inv.on_hand,
            image_paths=image_paths,
            existing_offer_id=pm.ebay_offer_id,
            item_specifics=ebay_item_specifics,
        )

        pm.ebay_inventory_sku = ebay_res.get("ebay_inventory_sku")
        pm.ebay_offer_id = ebay_res.get("ebay_offer_id")
        pm.ebay_listing_id = ebay_res.get("ebay_listing_id")

        db.commit()

        return {
            "sku": sku,
            "on_hand": inv.on_hand,
            "square": square_res,
            "ebay": ebay_res,
        }
