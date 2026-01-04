from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

EBAY_API_BASE = "https://api.ebay.com"
EBAY_MEDIA_BASE = "https://apim.ebay.com"  # Media API host


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def _guess_image_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    return "application/octet-stream"


def _extract_offer_id_from_offer_exists_error(body_text: str) -> str | None:
    try:
        data = httpx.Response(200, content=body_text).json()
    except Exception:
        return None

    errors = data.get("errors") or []
    for err in errors:
        if err.get("errorId") == 25002:
            for p in err.get("parameters") or []:
                if p.get("name") == "offerId" and p.get("value"):
                    return str(p["value"])
    return None


def _to_aspects(item_specifics: dict[str, str] | None) -> dict[str, list[str]] | None:
    if not item_specifics:
        return None

    aspects: dict[str, list[str]] = {}
    for k, v in item_specifics.items():
        k = (k or "").strip()
        v = (v or "").strip()
        if not k or not v:
            continue
        aspects[k] = [v]
    return aspects or None


@dataclass
class EbayToken:
    access_token: str
    expires_at: datetime


@dataclass
class EbayClient:
    client_id: str
    client_secret: str
    refresh_token: str

    _cached_token: EbayToken | None = None

    async def get_access_token(self) -> str:
        return await self._get_user_access_token()

    async def _get_user_access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._cached_token and self._cached_token.expires_at > now + timedelta(minutes=2):
            return self._cached_token.access_token

        url = f"{EBAY_API_BASE}/identity/v1/oauth2/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _basic_auth_header(self.client_id, self.client_secret),
        }

        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
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
            expires_at = now + timedelta(seconds=expires_in)

            self._cached_token = EbayToken(access_token=access_token, expires_at=expires_at)
            return access_token

    async def _headers(
        self,
        *,
        content_type: str | None = None,
        content_language: str | None = None,
    ) -> dict[str, str]:
        token = await self._get_user_access_token()
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type
        if content_language:
            headers["Content-Language"] = content_language
        return headers

    # ---------- Media API (images) ----------

    async def upload_image_from_file(self, image_path: Path) -> str:
        url = f"{EBAY_MEDIA_BASE}/commerce/media/v1_beta/image/create_image_from_file"
        ctype = _guess_image_content_type(image_path)
        files = {"image": (image_path.name, image_path.read_bytes(), ctype)}

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers=await self._headers(), files=files)
            if r.status_code >= 400:
                raise RuntimeError(f"eBay createImageFromFile failed: HTTP {r.status_code}: {r.text}")

            payload = r.json()
            eps_url = payload.get("imageUrl")
            if not eps_url:
                raise RuntimeError(f"eBay createImageFromFile missing imageUrl: {payload}")
            return eps_url

    # ---------- Inventory API (item/offer/publish) ----------

    async def create_or_replace_inventory_item(
        self,
        sku: str,
        title: str,
        description: str,
        image_urls: list[str],
        condition: str,
        quantity: int,
        item_specifics: dict[str, str] | None = None,
    ) -> None:
        url = f"{EBAY_API_BASE}/sell/inventory/v1/inventory_item/{sku}"

        product: dict[str, Any] = {
            "title": title,
            "description": description,
            "imageUrls": image_urls,
        }

        aspects = _to_aspects(item_specifics)
        if aspects:
            product["aspects"] = aspects

        payload: dict[str, Any] = {
            "availability": {"shipToLocationAvailability": {"quantity": int(quantity)}},
            "condition": condition,
            "product": product,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.put(
                url,
                headers=await self._headers(content_type="application/json", content_language="en-GB"),
                json=payload,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"eBay createOrReplaceInventoryItem failed: HTTP {r.status_code}: {r.text}")

    async def _put_offer(self, offer_id: str, payload: dict[str, Any]) -> str:
        url = f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.put(
                url,
                headers=await self._headers(content_type="application/json", content_language="en-GB"),
                json=payload,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"eBay offer replace failed: HTTP {r.status_code}: {r.text}")
        return offer_id

    async def get_offer(self, offer_id: str) -> dict[str, Any]:
        """
        Truth source for availableQuantity.
        """
        url = f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, headers=await self._headers(content_language="en-GB"))
            if r.status_code >= 400:
                raise RuntimeError(f"eBay get offer failed: HTTP {r.status_code}: {r.text}")
            return r.json()

    async def get_inventory_item(self, sku: str) -> dict[str, Any]:
        url = f"{EBAY_API_BASE}/sell/inventory/v1/inventory_item/{sku}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, headers=await self._headers(content_language="en-GB"))
            if r.status_code >= 400:
                raise RuntimeError(f"eBay get inventory item failed: HTTP {r.status_code}: {r.text}")
            return r.json()

    async def create_or_replace_offer(
        self,
        *,
        offer_id: str | None,
        sku: str,
        marketplace_id: str,
        merchant_location_key: str,
        category_id: str,
        listing_description: str,
        price_gbp: float,
        quantity: int,
        fulfillment_policy_id: str,
        payment_policy_id: str,
        return_policy_id: str,
    ) -> str:
        payload: dict[str, Any] = {
            "sku": sku,
            "marketplaceId": marketplace_id,
            "merchantLocationKey": merchant_location_key,
            "format": "FIXED_PRICE",
            "availableQuantity": int(quantity),
            "categoryId": str(category_id),
            "listingDescription": listing_description,
            "pricingSummary": {"price": {"value": f"{price_gbp:.2f}", "currency": "GBP"}},
            "listingPolicies": {
                "fulfillmentPolicyId": fulfillment_policy_id,
                "paymentPolicyId": payment_policy_id,
                "returnPolicyId": return_policy_id,
            },
        }

        if offer_id:
            return await self._put_offer(str(offer_id), payload)

        url = f"{EBAY_API_BASE}/sell/inventory/v1/offer"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                url,
                headers=await self._headers(content_type="application/json", content_language="en-GB"),
                json=payload,
            )

            if r.status_code < 400:
                data = r.json()
                new_offer_id = data.get("offerId")
                if not new_offer_id:
                    raise RuntimeError(f"eBay offer response missing offerId: {data}")
                return str(new_offer_id)

            if r.status_code == 400:
                existing_offer_id = _extract_offer_id_from_offer_exists_error(r.text)
                if existing_offer_id:
                    return await self._put_offer(existing_offer_id, payload)

            raise RuntimeError(f"eBay offer create/replace failed: HTTP {r.status_code}: {r.text}")

    async def publish_offer(self, offer_id: str) -> str:
        url = f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}/publish"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=await self._headers(content_type="application/json"))
            if r.status_code >= 400:
                raise RuntimeError(f"eBay publishOffer failed: HTTP {r.status_code}: {r.text}")
            data = r.json()
            listing_id = data.get("listingId") or data.get("listing_id")
            return str(listing_id) if listing_id else ""

    async def bulk_update_price_quantity(
        self,
        *,
        sku: str,
        offer_id: str,
        merchant_location_key: str,
        quantity: int,
    ) -> dict[str, Any]:
        url = f"{EBAY_API_BASE}/sell/inventory/v1/bulk_update_price_quantity"
        payload = {
            "requests": [
                {
                    "sku": str(sku),
                    "shipToLocationAvailability": {
                        "quantity": int(quantity),
                        "availabilityDistributions": [
                            {"merchantLocationKey": merchant_location_key, "quantity": int(quantity)}
                        ],
                    },
                    "offers": [{"offerId": str(offer_id), "availableQuantity": int(quantity)}],
                }
            ]
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                url,
                headers=await self._headers(content_type="application/json", content_language="en-GB"),
                json=payload,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"eBay bulk_update_price_quantity failed: HTTP {r.status_code}: {r.text}")
            return r.json()

    # ---------- Inventory API (listing migration) ----------

    # inside EbayClient
    async def bulk_migrate_listing(self, listing_ids: list[str]) -> dict[str, Any]:
        url = f"{EBAY_API_BASE}/sell/inventory/v1/bulk_migrate_listing"
        payload = {"requests": [{"listingId": str(i)} for i in listing_ids]}

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                url,
                headers=await self._headers(content_type="application/json", content_language="en-GB"),
                json=payload,
            )

            # 409 usually means "already migrated" — return body so caller can recover.
            if r.status_code == 409:
                try:
                    return r.json()
                except Exception:
                    return {"statusCode": 409, "body": r.text}

            if r.status_code >= 400:
                raise RuntimeError(f"eBay bulk_migrate_listing failed: HTTP {r.status_code}: {r.text}")

            return r.json()

