from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


def _mime_for_path(p: Path) -> str:
    mime, _ = mimetypes.guess_type(str(p))
    if mime in {"image/jpeg", "image/pjpeg", "image/png", "image/gif", "image/x-png"}:
        return mime
    ext = p.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    return "application/octet-stream"


class SquareClient:
    def __init__(self) -> None:
        self.base_url = "https://connect.squareup.com"
        self.location_id = settings.square_location_id
        self.version = settings.square_version
        self.token = settings.square_access_token

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Square-Version": self.version,
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    async def retrieve_order(self, order_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/orders/{order_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, headers=self._headers("application/json"))
            if r.status_code >= 400:
                raise RuntimeError(f"Square retrieve order failed: HTTP {r.status_code}: {r.text}")
            return r.json()

    async def retrieve_payment(self, payment_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/payments/{payment_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, headers=self._headers("application/json"))
            if r.status_code >= 400:
                raise RuntimeError(f"Square retrieve payment failed: HTTP {r.status_code}: {r.text}")
            return r.json()

    @staticmethod
    def verify_webhook_signature(
        *,
        signature_key: str,
        notification_url: str,
        raw_body: bytes,
        provided_signature: str,
    ) -> bool:
        """
        expected = base64(hmac_sha256(signature_key, notification_url + raw_body))
        header = x-square-hmacsha256-signature
        """
        msg = notification_url.encode("utf-8") + raw_body
        digest = hmac.new(signature_key.encode("utf-8"), msg, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, provided_signature or "")
