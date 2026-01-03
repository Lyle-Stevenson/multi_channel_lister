import asyncio
import os
from pathlib import Path

import httpx


async def main():
    # This script calls the running API container.
    # Put images in ./scripts/test_images/
    img_dir = Path("scripts/test_images")
    imgs = list(img_dir.glob("*.*"))

    if not imgs:
        print("Put at least 1 image in scripts/test_images/ (jpg/png/gif).")
        return

    data = {
        "sku": "TEST-SKU-001",
        "name": "Test Product From API",
        "price_gbp": "12.34",
        "quantity": "5",
        "description": "Created via Docker+VSCode integration test",
    }

    files = [("images", (p.name, p.read_bytes(), "application/octet-stream")) for p in imgs]

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("http://localhost:8000/square/upsert", data=data, files=files)
        print("Status:", r.status_code)
        print(r.text)


if __name__ == "__main__":
    asyncio.run(main())
