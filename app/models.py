from __future__ import annotations

from datetime import datetime
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, DateTime

from app.db import Base


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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Inventory(Base):
    __tablename__ = "inventory"

    sku: Mapped[str] = mapped_column(String(80), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
