from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import Base, engine, SessionLocal
from app.models import ProductMap, Inventory, WebhookEvent

from app.square_client import SquareClient
from app.square_service import SquareService

from app.ebay_client import EbayClient
from app.ebay_service import EbayService

from app.multi_service import MultiChannelService

from app.square_webhook import (
    verify_square_signature,
    extract_payment_order_id_and_status,
    extract_inventory_change,
    apply_square_order_and_sync_ebay,
    apply_square_inventory_change_and_sync_ebay,
)

from app.ebay_platform_webhook import (
    parse_ebay_platform_notification,
    _lookup_product_map,
    apply_ebay_item_revised_and_sync_square,
    apply_ebay_fixed_price_txn_and_sync_square,
)

app = FastAPI(title="Multi-Channel Lister (Square + eBay UK)")


async def _wait_for_db_and_init(max_attempts: int = 30) -> None:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            Base.metadata.create_all(bind=engine)
            return
        except OperationalError as e:
            last_err = e
            await asyncio.sleep(min(1.5 * attempt, 10))
    raise RuntimeError(f"Database not ready after {max_attempts} attempts: {last_err}")


@app.on_event("startup")
async def startup_event():
    settings.validate_required()
    await _wait_for_db_and_init()


square_client = SquareClient(access_token=settings.square_access_token, version=settings.square_version)
square_service = SquareService(client=square_client, location_id=settings.square_location_id)

ebay_client = EbayClient(
    client_id=settings.ebay_client_id,
    client_secret=settings.ebay_client_secret,
    refresh_token=settings.ebay_refresh_token,
)
ebay_service = EbayService(
    client=ebay_client,
    marketplace_id=settings.ebay_marketplace_id,
    merchant_location_key=settings.ebay_merchant_location_key,
    fulfillment_policy_id=settings.ebay_fulfillment_policy_id,
    payment_policy_id=settings.ebay_payment_policy_id,
    return_policy_id=settings.ebay_return_policy_id,
)

multi_service = MultiChannelService(square=square_service, ebay=ebay_service)


def _safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _provider_error(provider: str, exc: Exception, status_code: int = 502):
    msg = str(exc)
    body = None
    if "HTTP" in msg and ":" in msg:
        body = msg.split(":", 1)[1].strip()

    payload = _safe_json_loads(body) if body else None
    if payload is None:
        payload = {"message": msg}
    return JSONResponse(status_code=status_code, content={"ok": False, "provider": provider, "error": payload})


_CONDITION_ID_TO_ENUM = {
    1000: "NEW",
    1500: "NEW_OTHER",
    1750: "NEW_WITH_DEFECTS",
    2000: "CERTIFIED_REFURBISHED",
    2500: "SELLER_REFURBISHED",
    3000: "USED_EXCELLENT",
    4000: "USED_VERY_GOOD",
    5000: "USED_GOOD",
    6000: "USED_ACCEPTABLE",
    7000: "FOR_PARTS_OR_NOT_WORKING",
}


def _normalize_condition(condition_raw: str) -> str:
    s = (condition_raw or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="condition_id/condition is required")
    if s.isdigit():
        cid = int(s)
        enum_val = _CONDITION_ID_TO_ENUM.get(cid)
        if not enum_val:
            raise HTTPException(status_code=400, detail=f"Unsupported numeric condition_id {cid}")
        return enum_val
    return s


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/listings/upsert")
async def listings_upsert(
    sku: Annotated[str, Form()],
    title: Annotated[str, Form()],
    price_gbp: Annotated[float, Form()],
    quantity: Annotated[int, Form()],
    description: Annotated[str, Form()],
    square_reporting_category: Annotated[str | None, Form()] = None,
    ebay_category_id: Annotated[str, Form()] = "261055",
    ebay_condition: Annotated[str, Form()] = "NEW",
    ebay_item_specifics_json: Annotated[str | None, Form()] = None,
    images: list[UploadFile] = File(default_factory=list),
):
    if not images:
        raise HTTPException(status_code=400, detail="At least 1 image is required")

    ebay_condition_enum = _normalize_condition(ebay_condition)

    ebay_item_specifics: dict[str, str] | None = None
    if ebay_item_specifics_json:
        parsed = _safe_json_loads(ebay_item_specifics_json)
        if parsed is None or not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="ebay_item_specifics_json must be JSON object")
        ebay_item_specifics = {str(k): str(v) for k, v in parsed.items()}

    temp_paths: list[Path] = []
    try:
        tmp_dir = Path("/tmp/uploads")
        tmp_dir.mkdir(parents=True, exist_ok=True)

        for f in images:
            if not f.filename:
                continue
            p = tmp_dir / f.filename
            p.write_bytes(await f.read())
            temp_paths.append(p)

        with SessionLocal() as db:
            try:
                result = await multi_service.upsert_both(
                    db=db,
                    sku=sku.strip(),
                    title=title.strip(),
                    price_gbp=float(price_gbp),
                    quantity=int(quantity),
                    description_html=description,
                    image_paths=temp_paths,
                    square_reporting_category=square_reporting_category,
                    ebay_category_id=ebay_category_id.strip(),
                    ebay_condition=ebay_condition_enum,
                    ebay_item_specifics=ebay_item_specifics,
                )
                return {"ok": True, **result}
            except Exception as e:
                s = str(e)
                if "Square" in s or "square" in s:
                    return _provider_error("square", e, status_code=400 if "HTTP 400" in s else 502)
                if "eBay" in s or "ebay" in s:
                    return _provider_error("ebay", e, status_code=400 if "HTTP 400" in s else 502)
                return _provider_error("internal", e, status_code=500)

    finally:
        for p in temp_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


@app.get("/products")
def list_products():
    with SessionLocal() as db:
        rows = db.execute(select(ProductMap).order_by(ProductMap.updated_at.desc())).scalars().all()
        invs = {i.sku: i.on_hand for i in db.execute(select(Inventory)).scalars().all()}
        return [
            {
                "sku": r.sku,
                "name": r.name,
                "on_hand": invs.get(r.sku, 0),
                "square_item_id": r.square_item_id,
                "square_variation_id": r.square_variation_id,
                "ebay_inventory_sku": r.ebay_inventory_sku,
                "ebay_offer_id": r.ebay_offer_id,
                "ebay_listing_id": r.ebay_listing_id,
                "updated_at": str(r.updated_at),
            }
            for r in rows
        ]


async def _process_square_paid(event_id: str, event_type: str, order_id: str) -> dict:
    with SessionLocal() as db:
        return await apply_square_order_and_sync_ebay(db=db, event_id=event_id, event_type=event_type, order_id=order_id)


async def _process_square_inventory(event_id: str, event_type: str, changes: list[dict]) -> dict:
    with SessionLocal() as db:
        return await apply_square_inventory_change_and_sync_ebay(db=db, event_id=event_id, event_type=event_type, changes=changes)


# -------------------------
# eBay Platform Notifications (inline debug)
# -------------------------
async def _process_ebay_platform_event(raw_body: bytes) -> dict:
    print("EBAY PLATFORM: raw_len =", len(raw_body))

    try:
        ev = parse_ebay_platform_notification(raw_body)
    except Exception as e:
        print("EBAY PLATFORM: parse FAILED:", repr(e))
        return {"action": "parse_failed", "error": repr(e)}

    event_id = ev.correlation_id or f"ebay_platform:{ev.event_name}:{ev.sku or 'nosku'}:{ev.item_id or 'noitem'}"
    print(
        "EBAY PLATFORM: parsed event_name=",
        ev.event_name,
        "correlation_id=",
        ev.correlation_id,
        "event_id=",
        event_id,
        "sku=",
        ev.sku,
        "item_id=",
        ev.item_id,
        "qty=",
        ev.quantity,
        "qty_sold=",
        ev.quantity_sold,
        "qty_purchased=",
        ev.quantity_purchased,
    )

    with SessionLocal() as db:
        existing = db.get(WebhookEvent, event_id)
        if existing and existing.applied_inventory:
            print("EBAY PLATFORM: duplicate event; already applied:", event_id)
            return {"event": ev.event_name, "event_id": event_id, "action": "duplicate_ignored"}

        if not existing:
            existing = WebhookEvent(event_id=event_id, provider="ebay_platform", event_type=ev.event_name, order_id=None)
            db.add(existing)
            db.commit()

        pm = _lookup_product_map(db, sku=ev.sku, item_id=ev.item_id)
        if not pm or not pm.square_variation_id:
            print("EBAY PLATFORM: no mapping found (sku/item_id):", ev.sku, ev.item_id)
            existing.applied_inventory = True
            db.commit()
            return {
                "event": ev.event_name,
                "event_id": event_id,
                "sku": ev.sku,
                "item_id": ev.item_id,
                "action": "ignored_no_mapping",
            }

        updated: dict | None = None

        if ev.event_name == "ItemRevised":
            if ev.quantity is None:
                existing.applied_inventory = True
                db.commit()
                return {"event": ev.event_name, "event_id": event_id, "sku": pm.sku, "action": "ignored_missing_quantity"}

            updated = await apply_ebay_item_revised_and_sync_square(
                db=db,
                event_id=event_id,
                pm=pm,
                quantity=int(ev.quantity),
                quantity_sold=int(ev.quantity_sold or 0),
            )

        elif ev.event_name == "FixedPriceTransaction":
            if ev.quantity_purchased is None:
                existing.applied_inventory = True
                db.commit()
                return {
                    "event": ev.event_name,
                    "event_id": event_id,
                    "sku": pm.sku,
                    "action": "ignored_missing_quantity_purchased",
                }

            updated = await apply_ebay_fixed_price_txn_and_sync_square(
                db=db,
                event_id=event_id,
                pm=pm,
                qty_purchased=int(ev.quantity_purchased),
            )

        else:
            existing.applied_inventory = True
            db.commit()
            return {"event": ev.event_name, "event_id": event_id, "sku": pm.sku, "action": "ignored_unhandled_event"}

        square_status = "skipped"
        try:
            await square_service.set_stock_exact(
                variation_id=updated["square_variation_id"],
                new_quantity=updated["after"],
            )
            square_status = "updated"
        except Exception as e:
            print("EBAY PLATFORM: Square set_stock_exact FAILED:", repr(e))
            square_status = "failed"

        existing.applied_inventory = True
        db.commit()

        print("EBAY PLATFORM: applied:", updated, "square:", square_status)
        return {
            "event": ev.event_name,
            "event_id": event_id,
            "sku": pm.sku,
            "action": "applied",
            "updated": updated,
            "square": square_status,
        }


@app.post("/webhooks/ebay/platform/kdfos45rfs")
async def ebay_platform_webhook(request: Request, background_tasks: BackgroundTasks):
    raw = await request.body()
    background_tasks.add_task(_process_ebay_platform_event, raw)
    return {"ok": True}


@app.post("/webhooks/square")
async def square_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_square_hmacsha256_signature: Annotated[str | None, Header(alias="x-square-hmacsha256-signature")] = None,
):
    raw_body = await request.body()

    if not verify_square_signature(raw_body=raw_body, signature=x_square_hmacsha256_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = _safe_json_loads(raw_body.decode("utf-8")) or {}
    event_id = payload.get("event_id") or payload.get("eventId") or payload.get("id")
    event_type = payload.get("type") or payload.get("event_type") or ""

    print("square webhook type:", event_type, "event_id:", event_id)
    print("inventory changes:", extract_inventory_change(payload))

    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event_id")

    # payment flow -> order decrement
    order_id, status = extract_payment_order_id_and_status(payload)
    if order_id and (status or "").upper() == "COMPLETED":
        background_tasks.add_task(_process_square_paid, str(event_id), str(event_type), str(order_id))
        return {"ok": True}

    changes = extract_inventory_change(payload)
    if changes:
        background_tasks.add_task(_process_square_inventory, str(event_id), str(event_type), changes)
        return {"ok": True}

    return {"ok": True}
