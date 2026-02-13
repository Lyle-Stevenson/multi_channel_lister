from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Any

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


def _safe_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_sku(payload: Any) -> str | None:
    """
    Best-effort extraction of SKU from various response shapes.
    Prefer top-level 'sku', else look into common nested keys.
    """
    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("sku"), str) and payload["sku"].strip():
        return payload["sku"].strip()

    # Common nested patterns
    for key in ("product", "item", "result", "updated", "mapping"):
        v = payload.get(key)
        if isinstance(v, dict):
            sku = v.get("sku")
            if isinstance(sku, str) and sku.strip():
                return sku.strip()

    return None

def find_and_order_images(folder: str) -> list[Path]:
    d = Path(folder)
    if not d.exists() or not d.is_dir():
        raise SystemExit(f"ERROR: folder does not exist or is not a directory: {folder}")

    files = [p for p in d.iterdir() if p.is_file()]
    # Require an image named "front" (case-insensitive), any allowed extension.
    front = None
    for p in files:
        if p.stem.lower() == "front" and p.suffix.lower() in IMAGE_EXTS:
            front = p
            break

    if front is None:
        raise SystemExit(
            "ERROR: Missing required front image. "
            "Add an image named 'front' (e.g. front.jpg / front.png) in the folder."
        )

    # Remaining images (exclude front), stable sorted by name
    rest = sorted(
        [p for p in files if p != front and p.suffix.lower() in IMAGE_EXTS],
        key=lambda x: x.name.lower(),
    )

    return [front] + rest


def main() -> int:

    EBAY_TITLE_MAX_LEN = 80

    p = argparse.ArgumentParser(description="List/Update on Square + eBay UK in one call (shared inventory).")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument(
        "--sku",
        required=False,
        default=None,
        help="Optional. If omitted, the API generates the next SKU automatically.",
    )
    p.add_argument("--title", required=True, help="Product title for both Square and eBay (<= 80 chars)")
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
        images = find_and_order_images(args.folder)  # front.* first, error if missing
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # images will always be non-empty if front is found, but keep a guard anyway
    if not images:
        print("ERROR: No images found.", file=sys.stderr)
        return 2

    specifics = parse_specifics(args.specific) if args.specific else {}

    url = args.api.rstrip("/") + "/listings/upsert"

    title = (args.title or "").strip()

    if not title:
        print("ERROR: Provide --title", file=sys.stderr)
        return 2

    if len(title) > EBAY_TITLE_MAX_LEN:
        print(f"ERROR: Title must be <= {EBAY_TITLE_MAX_LEN} chars (got {len(title)})", file=sys.stderr)
        return 2

    data: dict[str, str] = {
        "square_title": title,
        "ebay_title": title,
        "price_gbp": str(args.price),
        "quantity": str(args.qty),
        "description": args.desc,
        "ebay_category_id": args.ebay_category,
        "ebay_condition": args.ebay_condition,
    }

    if args.sku and str(args.sku).strip():
        data["sku"] = str(args.sku).strip()

    if args.square_category and str(args.square_category).strip():
        data["square_reporting_category"] = str(args.square_category).strip()

    if specifics:
        data["ebay_item_specifics_json"] = json.dumps(specifics)

    files = build_multipart_files(images)

    print(f"POST {url}")
    print(f"Uploading {len(images)} image(s) from: {folder}")
    if "sku" in data:
        print(f"SKU: {data['sku']} (provided)")
    else:
        print("SKU: (auto-generated by API)")

    with httpx.Client(timeout=300) as client:
        r = client.post(url, data=data, files=files)

    print(f"\nStatus: {r.status_code}")

    payload = _safe_json(r.text)
    if payload is not None:
        sku = _extract_sku(payload)
        if sku:
            print(f"Returned SKU: {sku}")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(r.text)

    return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
