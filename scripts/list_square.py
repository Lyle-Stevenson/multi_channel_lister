from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

import httpx


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def iter_images(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Folder does not exist or is not a directory: {folder}")

    files: list[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            files.append(p)

    # Stable ordering so results are consistent
    files.sort(key=lambda x: x.name.lower())
    return files


def build_multipart_files(image_paths: Iterable[Path]):
    """
    Returns a list of ("images", (filename, bytes, content_type)) tuples for multipart upload.
    """
    files = []
    for p in image_paths:
        # Minimal content type mapping
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List/Update an item on Square via the local API, uploading all images from a folder."
    )
    parser.add_argument("--api", default="http://localhost:8000", help="Base API URL (default: http://localhost:8000)")
    parser.add_argument("--sku", required=True, help="SKU (your identifier)")
    parser.add_argument("--name", required=True, help="Item name")
    parser.add_argument("--price", required=True, type=float, help="Price in GBP, e.g. 12.34")
    parser.add_argument("--qty", required=True, type=int, help="Quantity, e.g. 5")
    parser.add_argument("--desc", default=None, help="Description (optional)")
    parser.add_argument("--category", default=None, help="Square reporting_category_id (optional)")
    parser.add_argument("--folder", required=True, help="Folder containing images (jpg/jpeg/png/gif/webp)")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of images uploaded (0 = no limit)")
    parser.add_argument("--timeout", type=float, default=180.0, help="Request timeout in seconds (default 180)")

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

    url = args.api.rstrip("/") + "/square/upsert"

    data = {
        "sku": args.sku,
        "name": args.name,
        "price_gbp": str(args.price),
        "quantity": str(args.qty),
    }
    if args.desc is not None:
        data["description"] = args.desc
    if args.category is not None and args.category.strip():
        data["reporting_category_id"] = args.category.strip()

    files = build_multipart_files(images)

    print(f"POST {url}")
    print(f"Uploading {len(images)} image(s) from: {folder}")
    for p in images:
        print(f" - {p.name}")

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
