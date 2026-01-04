from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

import httpx

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def iter_images(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Folder does not exist or is not a directory: {folder}")
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    files.sort(key=lambda x: x.name.lower())
    return files


def build_multipart_files(image_paths: Iterable[Path]):
    files = []
    for p in image_paths:
        ext = p.suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            ctype = "image/jpeg"
        elif ext == ".png":
            ctype = "image/png"
        elif ext == ".gif":
            ctype = "image/gif"
        elif ext == ".webp":
            ctype = "image/webp"
        else:
            ctype = "application/octet-stream"

        files.append(("images", (p.name, p.read_bytes(), ctype)))
    return files


def parse_specifics(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in pairs:
        if "=" not in s:
            raise ValueError(f"Invalid --specific '{s}'. Use Name=Value")
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or not v:
            raise ValueError(f"Invalid --specific '{s}'.")
        out[k] = v
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="List/Update on Square + eBay UK in one call (shared inventory).")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--title", required=True)
    p.add_argument("--price", required=True, type=float)
    p.add_argument("--qty", required=True, type=int)
    p.add_argument("--desc", required=True)
    p.add_argument("--folder", required=True)

    p.add_argument("--square-category", default=None, help="Square reporting category name (optional)")

    p.add_argument("--ebay-category", default="261055")
    p.add_argument("--ebay-condition", default="NEW")
    p.add_argument("--specific", action="append", default=[])

    args = p.parse_args()

    folder = Path(args.folder)
    try:
        images = iter_images(folder)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not images:
        print("ERROR: No images found.", file=sys.stderr)
        return 2

    specifics = parse_specifics(args.specific) if args.specific else {}

    url = args.api.rstrip("/") + "/listings/upsert"
    data = {
        "sku": args.sku,
        "title": args.title,
        "price_gbp": str(args.price),
        "quantity": str(args.qty),
        "description": args.desc,
        "square_reporting_category": args.square_category or "",
        "ebay_category_id": args.ebay_category,
        "ebay_condition": args.ebay_condition,
    }
    if specifics:
        data["ebay_item_specifics_json"] = json.dumps(specifics)

    files = build_multipart_files(images)

    print(f"POST {url}")
    print(f"Uploading {len(images)} image(s) from: {folder}")

    with httpx.Client(timeout=300) as client:
        r = client.post(url, data=data, files=files)

    print(f"\nStatus: {r.status_code}")
    print(r.text)
    return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
