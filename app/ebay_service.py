from __future__ import annotations

from pathlib import Path
from typing import Any

from app.ebay_client import EbayClient


def _to_int(x: object, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(x)  # handles strings like "2"
    except Exception:
        return default


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
        eps_urls: list[str] = []
        for p in image_paths:
            eps_url = await self.client.upload_image_from_file(p)
            eps_urls.append(eps_url)

        await self.client.create_or_replace_inventory_item(
            sku=sku,
            title=title,
            description=description,
            image_urls=eps_urls,
            condition=condition,
            quantity=int(quantity),
            item_specifics=item_specifics,
        )

        offer_id = await self.client.create_or_replace_offer(
            offer_id=existing_offer_id,
            sku=sku,
            marketplace_id=self.marketplace_id,
            merchant_location_key=self.merchant_location_key,
            category_id=category_id,
            listing_description=description,
            price_gbp=price_gbp,
            quantity=int(quantity),
            fulfillment_policy_id=self.fulfillment_policy_id,
            payment_policy_id=self.payment_policy_id,
            return_policy_id=self.return_policy_id,
        )

        listing_id = await self.client.publish_offer(offer_id)

        return {
            "ebay_inventory_sku": sku,
            "ebay_offer_id": offer_id,
            "ebay_listing_id": listing_id,
            "ebay_eps_image_urls": eps_urls,
            "ebay_condition": condition,
            "ebay_item_specifics": item_specifics or {},
        }

    async def update_quantity_only(self, *, sku: str, offer_id: str, new_quantity: int) -> dict[str, Any]:
        return await self.client.bulk_update_price_quantity(
            sku=sku,
            offer_id=offer_id,
            merchant_location_key=self.merchant_location_key,
            quantity=int(new_quantity),
        )

    async def get_offer_available_quantity(self, offer_id: str) -> int:
        """
        Authoritative read for availableQuantity.
        """
        data = await self.client.get_offer(str(offer_id))
        return _to_int(data.get("availableQuantity"), default=0)
    
    async def get_inventory_item_available_quantity(self, sku: str) -> int:
        """
        UI edits often show up here first:
          inventory_item.availability.shipToLocationAvailability.quantity
        """
        data = await self.client.get_inventory_item(str(sku))
        avail = (data.get("availability") or {}).get("shipToLocationAvailability") or {}
        return _to_int(avail.get("quantity"), default=0)

    async def delete_listing(self, *, offer_id: str | None, sku: str | None) -> dict[str, bool]:
        """
        Fully delete an eBay listing: withdraw the offer (end listing), delete offer, delete inventory item.
        """
        offer_deleted = False
        inventory_deleted = False

        if offer_id:
            try:
                await self.client.withdraw_offer(str(offer_id))
            except Exception as e:
                print(f"EBAY SERVICE: withdraw_offer failed (continuing): {repr(e)}")

            try:
                await self.client.delete_offer(str(offer_id))
                offer_deleted = True
            except Exception as e:
                print(f"EBAY SERVICE: delete_offer failed: {repr(e)}")

        if sku:
            try:
                await self.client.delete_inventory_item(str(sku))
                inventory_deleted = True
            except Exception as e:
                print(f"EBAY SERVICE: delete_inventory_item failed: {repr(e)}")

        return {"offer_deleted": offer_deleted, "inventory_deleted": inventory_deleted}
