from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProductMap(Base):
    __tablename__ = "product_map"

    sku: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")

    # Square
    square_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    square_variation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # eBay
    ebay_inventory_sku: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ebay_offer_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ebay_listing_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Inventory(Base):
    __tablename__ = "inventory"

    sku: Mapped[str] = mapped_column(String(80), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)

    # NEW: sync marker to avoid webhook echo loops
    # values: "square", "ebay" (you can extend)
    last_source: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    last_source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebhookEvent(Base):
    """
    Idempotency + retry support for incoming webhooks.
    - applied_inventory: inventory change has been applied once
    - ebay_synced: eBay quantity update succeeded (for Square webhooks)
    """
    __tablename__ = "webhook_event"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), default="square")
    event_type: Mapped[str] = mapped_column(String(80), default="")
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    applied_inventory: Mapped[bool] = mapped_column(Boolean, default=False)
    ebay_synced: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
