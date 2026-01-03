from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime, timezone
import uuid

import httpx


def _mime_for_path(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    return "application/octet-stream"


class SquareClient:
    def __init__(self, access_token: str, version: str = "2025-01-22"):
        if not access_token or not str(access_token).strip():
            raise RuntimeError("Square access token is missing. Set SQUARE_ACCESS_TOKEN in .env.")
        self.access_token = access_token
        self.version = version
        self.base_url = "https://connect.squareup.com/v2"

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.access_token}",
            "Square-Version": self.version,
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _idempotency_key(self) -> str:
        return str(uuid.uuid4())

    async def upsert_catalog_object(self, *, idempotency_key: str, catalog_object: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/catalog/object"
        payload = {"idempotency_key": idempotency_key, "object": catalog_object}

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=self._headers("application/json"), json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"Square upsert failed: HTTP {r.status_code}: {r.text}")
            return r.json()

    async def search_catalog_categories_by_name(self, *, name: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/catalog/search"
        payload = {
            "object_types": ["CATEGORY"],
            "query": {"text_query": {"keywords": [name]}}},
        payload["include_related_objects"] = False
        payload["limit"] = 50

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=self._headers("application/json"), json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"Square category search failed: HTTP {r.status_code}: {r.text}")
            data = r.json()
            return data.get("objects") or []

    async def create_or_get_category_id(self, *, category_name: str) -> str:
        name = category_name.strip()
        if not name:
            raise ValueError("category_name is empty")

        objs = await self.search_catalog_categories_by_name(name=name)
        for o in objs:
            if o.get("type") == "CATEGORY":
                cd = o.get("category_data") or {}
                if (cd.get("name") or "").strip().lower() == name.lower():
                    return o["id"]

        idem = f"cat-{name.lower().replace(' ', '-')}"
        cat_obj = {"type": "CATEGORY", "id": f"#{idem}", "category_data": {"name": name}}
        up = await self.upsert_catalog_object(idempotency_key=idem, catalog_object=cat_obj)
        return up["catalog_object"]["id"]

    async def create_catalog_image(
        self,
        image_path: Path,
        *,
        object_id: str,
        idempotency_key: str,
        is_primary: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/catalog/images"

        mime = _mime_for_path(image_path)
        if mime == "application/octet-stream":
            raise RuntimeError(
                f"Unsupported image type for Square: {image_path.name}. Use .jpg/.jpeg, .png, or .gif"
            )

        image_obj = {
            "type": "IMAGE",
            "id": f"#{idempotency_key}",
            "image_data": {"name": image_path.stem, "caption": image_path.name},
        }

        request_part = {
            "idempotency_key": idempotency_key,
            "image": image_obj,
            "object_id": object_id,
            "is_primary": bool(is_primary),
        }

        files = {
            "file": (image_path.name, image_path.read_bytes(), mime),
            "request": (None, httpx._content.json_dumps(request_part), "application/json"),
        }

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers=self._headers(), files=files)
            if r.status_code >= 400:
                raise RuntimeError(f"Square create image failed: HTTP {r.status_code}: {r.text}")

            data = r.json()
            img = data.get("image") or {}
            return {"image_id": img.get("id"), "raw": data}

    async def batch_adjust_inventory_in_stock(
        self,
        *,
        variation_id: str,
        location_id: str,
        delta_quantity: int,
        occurred_at: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """
        Adjust inventory by delta. Handles both increases and decreases safely.

        Positive delta: move NONE -> IN_STOCK
        Negative delta: move IN_STOCK -> NONE
        """
        delta = int(delta_quantity)
        if delta == 0:
            return {"ok": True, "delta": 0}

        url = f"{self.base_url}/inventory/changes/batch-create"

        if delta > 0:
            from_state = "NONE"
            to_state = "IN_STOCK"
            qty = str(delta)
        else:
            from_state = "IN_STOCK"
            to_state = "NONE"
            qty = str(abs(delta))

        payload = {
            "idempotency_key": idempotency_key,
            "changes": [
                {
                    "type": "ADJUSTMENT",
                    "adjustment": {
                        "catalog_object_id": variation_id,
                        "location_id": location_id,
                        "from_state": from_state,
                        "to_state": to_state,
                        "quantity": qty,
                        "occurred_at": occurred_at,
                    },
                }
            ],
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=self._headers("application/json"), json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"Square batch_adjust_inventory failed: HTTP {r.status_code}: {r.text}")
            return r.json()

    async def batch_set_inventory_physical_count_in_stock(
        self,
        *,
        variation_id: str,
        location_id: str,
        quantity: int,
        occurred_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """
        Set an absolute IN_STOCK quantity using PHYSICAL_COUNT.
        This is the most reliable way to force Square stock to match DB.
        """
        url = f"{self.base_url}/inventory/changes/batch-create"

        if not occurred_at:
            occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not idempotency_key:
            idempotency_key = self._idempotency_key()

        payload = {
            "idempotency_key": idempotency_key,
            "changes": [
                {
                    "type": "PHYSICAL_COUNT",
                    "physical_count": {
                        "catalog_object_id": str(variation_id),
                        "location_id": str(location_id),
                        "quantity": str(max(int(quantity), 0)),
                        "state": "IN_STOCK",
                        "occurred_at": occurred_at,
                    },
                }
            ],
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=self._headers("application/json"), json=payload)
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Square batch_set_inventory_physical_count_in_stock failed: HTTP {r.status_code}: {r.text}"
                )
            return r.json()
