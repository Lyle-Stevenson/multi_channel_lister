from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import html
import re

import httpx

from app.square_client import SquareClient


def _mapping(upsert_res: dict[str, Any], client_object_id: str) -> str | None:
    for m in (upsert_res.get("id_mappings") or []):
        if m.get("client_object_id") == client_object_id and m.get("object_id"):
            return str(m["object_id"])
    return None


def _variation_id_from_catalog_object(upsert_res: dict[str, Any]) -> str | None:
    obj = upsert_res.get("catalog_object") or {}
    if obj.get("type") != "ITEM":
        return None
    item_data = obj.get("item_data") or {}
    variations = item_data.get("variations") or []
    if not variations or not isinstance(variations, list):
        return None
    for v in variations:
        vid = v.get("id")
        if vid and not str(vid).startswith("#"):
            return str(vid)
    return None

_TAG_RE = re.compile(r"<[^>]+>")

def html_to_plain_text(s: str) -> str:
    if not s:
        return ""
    # Normalize common block tags to newlines first
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</\s*p\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*p(\s+[^>]*)?>", "", s)

    # Remove remaining tags
    s = _TAG_RE.sub("", s)

    # Decode HTML entities
    s = html.unescape(s)

    # Normalize whitespace / newlines
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)     # max 2 newlines
    s = re.sub(r"[ \t]{2,}", " ", s)     # collapse spaces
    return s.strip()


class SquareService:
    def __init__(self, client: SquareClient, location_id: str):
        if not location_id or not str(location_id).strip():
            raise RuntimeError("Square location id is missing. Set SQUARE_LOCATION_ID in .env.")
        self.client = client
        self.location_id = location_id

    async def delete_catalog_item(self, *, item_id: str) -> dict[str, Any]:
        """
        Deletes the Square ITEM. Variations are deleted automatically with the item.
        """
        return await self.client.delete_catalog_object(object_id=item_id)

    async def _get_current_in_stock(self, *, variation_id: str) -> int:
        url = f"{self.client.base_url}/inventory/counts/batch-retrieve"
        payload = {
            "catalog_object_ids": [variation_id],
            "location_ids": [self.location_id],
            "states": ["IN_STOCK"],
        }

        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.post(url, headers=self.client._headers("application/json"), json=payload)
            if r.status_code >= 400:
                return 0
            data = r.json()
            counts = data.get("counts") or []
            if not counts:
                return 0
            q = counts[0].get("quantity") or "0"
            try:
                return int(q)
            except Exception:
                return 0

    async def set_stock_exact(self, *, variation_id: str, new_quantity: int) -> dict[str, Any]:
        """
        Set IN_STOCK to an exact value by adjusting the delta safely.

        - If we need to increase: NONE -> IN_STOCK (qty = delta)
        - If we need to decrease: IN_STOCK -> SOLD (qty = abs(delta))

        This avoids the invalid negative-quantity adjustment you just hit.
        """
        target = max(int(new_quantity), 0)
        current = await self._get_current_in_stock(variation_id=variation_id)
        delta = target - current

        if delta == 0:
            return {"before": current, "after": target, "delta": 0, "method": "noop"}

        occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        idem = str(uuid.uuid4())

        if delta > 0:
            await self.client.batch_adjust_inventory(
                variation_id=variation_id,
                location_id=self.location_id,
                quantity=delta,
                from_state="NONE",
                to_state="IN_STOCK",
                occurred_at=occurred_at,
                idempotency_key=idem,
            )
            return {"before": current, "after": target, "delta": delta, "method": "adjust NONE->IN_STOCK"}

        # delta < 0 : reduce
        await self.client.batch_adjust_inventory(
            variation_id=variation_id,
            location_id=self.location_id,
            quantity=abs(delta),
            from_state="IN_STOCK",
            to_state="SOLD",  # (or "WASTE" if you prefer)
            occurred_at=occurred_at,
            idempotency_key=idem,
        )
        return {"before": current, "after": target, "delta": delta, "method": "adjust IN_STOCK->SOLD"}

    async def upsert_item_with_images_and_inventory(
        self,
        *,
        sku: str,
        name: str,
        description: str,
        price_gbp: float,
        quantity: int,
        image_paths: list[Path],
        reporting_category: str | None = None,
    ) -> dict[str, Any]:
        idem = str(uuid.uuid4())

        client_item_id = f"#item-{sku}-{idem}"
        client_var_id = f"#var-{sku}-{idem}"

        cat_id: str | None = None
        if reporting_category and reporting_category.strip():
            cat_id = await self.client.create_or_get_category_id(category_name=reporting_category.strip())

        item_data: dict[str, Any] = {
            "name": name,
            "description": html_to_plain_text(description),
            "variations": [
                {
                    "type": "ITEM_VARIATION",
                    "id": client_var_id,
                    "item_variation_data": {
                        "name": "Regular",
                        "sku": sku,
                        "pricing_type": "FIXED_PRICING",
                        "price_money": {"amount": int(round(float(price_gbp) * 100)), "currency": "GBP"},
                    },
                }
            ],
        }

        if cat_id:
            item_data["reporting_category"] = {"id": cat_id}

        catalog_item = {"type": "ITEM", "id": client_item_id, "item_data": item_data}
        upsert_res = await self.client.upsert_catalog_object(idempotency_key=idem, catalog_object=catalog_item)

        real_item_id = _mapping(upsert_res, client_item_id)
        real_var_id = _variation_id_from_catalog_object(upsert_res) or _mapping(upsert_res, client_var_id)

        if not real_item_id:
            obj = upsert_res.get("catalog_object") or {}
            if obj.get("id") and not str(obj["id"]).startswith("#"):
                real_item_id = str(obj["id"])

        if not real_item_id or str(real_item_id).startswith("#"):
            raise RuntimeError(f"Square did not return a real ITEM id. id_mappings={upsert_res.get('id_mappings')}")
        if not real_var_id or str(real_var_id).startswith("#"):
            raise RuntimeError(f"Square did not return a real VARIATION id. id_mappings={upsert_res.get('id_mappings')}")

        # Images
        image_ids: list[str] = []
        for idx, p in enumerate(image_paths):
            img_idem = str(uuid.uuid4())
            img_res = await self.client.create_catalog_image(
                p, object_id=real_item_id, idempotency_key=img_idem, is_primary=(idx == 0)
            )
            if img_res.get("image_id"):
                image_ids.append(img_res["image_id"])

        # Inventory (exact, works for both up + down)
        inv_res = await self.set_stock_exact(variation_id=real_var_id, new_quantity=int(quantity))

        return {
            "square_item_id": real_item_id,
            "square_variation_id": real_var_id,
            "square_image_ids": image_ids,
            "square_reporting_category": reporting_category or "",
            "square_inventory_before": inv_res.get("before"),
            "square_inventory_after": inv_res.get("after"),
            "square_inventory_delta": inv_res.get("delta"),
            "square_inventory_method": inv_res.get("method"),
        }