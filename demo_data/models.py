"""
models.py
---------
SQLAlchemy 2.0 ORM models for CMPIS.

Two tables:
- products: the catalog of tracked construction materials
- price_history: every vendor price snapshot ever collected for a product
  (currently populated by seed_demo_data.py; later by a real scraper
  without any change to this schema or to analytics.py/service.py).
"""

from __future__ import annotations

from datetime import datetime, date as date_type
from typing import List, Optional

from sqlalchemy import String, Float, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .database import Base


class Product(Base):
    """A construction material tracked by the system, e.g. 'UltraTech Cement 50kg'."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    brand: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    unit: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    # One product -> many historical price snapshots across vendors/dates.
    price_history: Mapped[List["PriceHistory"]] = relationship(
        "PriceHistory",
        back_populates="product",
        cascade="all, delete-orphan",
        lazy="selectin",  # efficient async-friendly eager load, avoids N+1 lazy-load errors
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product id={self.id} name={self.name!r} brand={self.brand!r}>"


class PriceHistory(Base):
    """A single vendor-quoted price snapshot for a product at a point in time."""

    __tablename__ = "price_history"
    __table_args__ = (
        # Speeds up the most common analytics query pattern: "give me all
        # rows for this product ordered by time".
        Index("ix_price_history_product_collected", "product_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(120), nullable=False, index=True)  # vendor/platform, e.g. "Moglix"
    seller: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # specific merchant/store name
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")
    purchase_link: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    availability: Mapped[str] = mapped_column(String(40), nullable=False, default="In Stock")
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    data_source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="demo", index=True
    )  # "demo" (simulated) or "live" (real Serper.dev scrape)

    # Many price_history rows -> one product.
    product: Mapped["Product"] = relationship(
        "Product",
        back_populates="price_history",
    )

    @property
    def collected_date(self) -> date_type:
        """Convenience accessor: just the calendar date part of collected_at."""
        return self.collected_at.date()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PriceHistory id={self.id} product_id={self.product_id} "
            f"source={self.source!r} price={self.price} collected_at={self.collected_at}>"
        )