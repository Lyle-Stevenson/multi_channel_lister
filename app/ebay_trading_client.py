# app/ebay_trading_client.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import httpx

TRADING_ENDPOINT = "https://api.ebay.com/ws/api.dll"

@dataclass
class EbayTradingClient:
    """
    Minimal Trading API client using OAuth access token.
    Requires your access token to include Trading scopes.
    """
    access_token_provider: Any  # expects .get_access_token() -> str

    async def _call(self, *, call_name: str, body_xml: str, site_id: str = "3") -> str:
        token = await self.access_token_provider.get_access_token()
        headers = {
            "Content-Type": "text/xml",
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": site_id,            # 3 = UK
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-IAF-TOKEN": token,           # OAuth token header for Trading
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(TRADING_ENDPOINT, headers=headers, content=body_xml.encode("utf-8"))
            r.raise_for_status()
            return r.text

    async def get_my_ebay_selling_active(self, *, page: int = 1, entries_per_page: int = 100) -> str:
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ActiveList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>{entries_per_page}</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
  </ActiveList>
</GetMyeBaySellingRequest>"""
        return await self._call(call_name="GetMyeBaySelling", body_xml=xml)

    async def get_item(self, *, item_id: str) -> str:
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
        return await self._call(call_name="GetItem", body_xml=xml)

    async def revise_item_set_sku(self, *, item_id: str, sku: str) -> str:
        # Works for single-variation listings.
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <Item>
    <ItemID>{item_id}</ItemID>
    <SKU>{sku}</SKU>
  </Item>
</ReviseItemRequest>"""
        return await self._call(call_name="ReviseItem", body_xml=xml)
