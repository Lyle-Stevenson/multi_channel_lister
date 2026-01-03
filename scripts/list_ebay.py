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
            raise ValueError(f"Invalid --specific '{s}'. Use Name=Value (e.g. Type=Figure)")
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or not v:
            raise ValueError(f"Invalid --specific '{s}'. Name and Value must be non-empty.")
        out[k] = v
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="List/Update an item on eBay UK via the local API, using all images in a folder.")
    parser.add_argument("--api", default="http://localhost:8000", help="Base API URL (default: http://localhost:8000)")
    parser.add_argument("--sku", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--price", required=True, type=float)
    parser.add_argument("--qty", required=True, type=int)
    parser.add_argument("--category", required=True, help="eBay categoryId (UK marketplace)")
    parser.add_argument("--condition", required=True, help='Use "NEW" etc, or legacy numeric like 1000')
    parser.add_argument("--desc", required=True, help="Listing description (HTML allowed)")
    parser.add_argument("--folder", required=True, help="Folder containing images")
    parser.add_argument("--specific", action="append", default=[], help='Repeatable. Example: --specific "Type=Figure"')
    parser.add_argument("--limit", type=int, default=0, help="Limit number of images uploaded (0 = no limit)")
    parser.add_argument("--timeout", type=float, default=300.0, help="Timeout seconds (default 300)")

    args = parser.parse_args()

    folder = Path(args.folder)
    try:
        images = iter_images(folder)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.limit and args.limit > 0:
        images = images[: args.limit]

    if not images:
        print(f"ERROR: No images found in {folder} (supported: {', '.join(sorted(IMAGE_EXTS))})", file=sys.stderr)
        return 2

    try:
        specifics = parse_specifics(args.specific)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    url = args.api.rstrip("/") + "/ebay/upsert"
    data = {
        "sku": args.sku,
        "title": args.title,
        "price_gbp": str(args.price),
        "quantity": str(args.qty),
        "category_id": args.category,
        "condition_id": str(args.condition),
        "description": args.desc,
    }
    if specifics:
        data["item_specifics_json"] = json.dumps(specifics)

    files = build_multipart_files(images)

    print(f"POST {url}")
    print(f"Uploading {len(images)} image(s) from: {folder}")
    for p in images:
        print(f" - {p.name}")
    if specifics:
        print("Item specifics:")
        for k, v in specifics.items():
            print(f" - {k} = {v}")

    try:
        with httpx.Client(timeout=args.timeout) as client:
            r = client.post(url, data=data, files=files)
    except Exception as e:
        print(f"ERROR: Request failed: {e}", file=sys.stderr)
        return 3

    print(f"\nStatus: {r.status_code}")
    print(r.text)
    return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
