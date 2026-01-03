from __future__ import annotations

from pathlib import Path
from typing import Any

from app.ebay_client import EbayClient


class EbayService:
    def __init__(
        self,
        client: EbayClient,
        *,
        marketplace_id: str,
        merchant_location_key: str,
        fulfillment_policy_id: str,
        payment_policy_id: str,
        return_policy_id: str,
    ):
        self.client = client
        self.marketplace_id = marketplace_id
        self.merchant_location_key = merchant_location_key
        self.fulfillment_policy_id = fulfillment_policy_id
        self.payment_policy_id = payment_policy_id
        self.return_policy_id = return_policy_id

    async def upsert_listing_with_images_and_inventory(
        self,
        *,
        sku: str,
        title: str,
        description: str,
        category_id: str,
        condition: str,
        price_gbp: float,
        quantity: int,
        image_paths: list[Path],
        existing_offer_id: str | None = None,
        item_specifics: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # 1) Upload local images -> EPS URLs
        eps_urls: list[str] = []
        for p in image_paths:
            eps_url = await self.client.upload_image_from_file(p)
            eps_urls.append(eps_url)

        # 2) Create/replace inventory item INCLUDING product aspects (item specifics)
        await self.client.create_or_replace_inventory_item(
            sku=sku,
            title=title,
            description=description,
            image_urls=eps_urls,
            condition=condition,
            quantity=quantity,
            item_specifics=item_specifics,
        )

        # 3) Create/replace offer (NO item_specifics here)
        offer_id = await self.client.create_or_replace_offer(
            offer_id=existing_offer_id,
            sku=sku,
            marketplace_id=self.marketplace_id,
            merchant_location_key=self.merchant_location_key,
            category_id=category_id,
            listing_description=description,
            price_gbp=price_gbp,
            quantity=quantity,
            fulfillment_policy_id=self.fulfillment_policy_id,
            payment_policy_id=self.payment_policy_id,
            return_policy_id=self.return_policy_id,
        )

        # 4) Publish offer -> listing
        listing_id = await self.client.publish_offer(offer_id)

        return {
            "ebay_inventory_sku": sku,
            "ebay_offer_id": offer_id,
            "ebay_listing_id": listing_id,
            "ebay_eps_image_urls": eps_urls,
            "ebay_condition": condition,
            "ebay_item_specifics": item_specifics or {},
        }
