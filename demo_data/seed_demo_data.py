"""
seed_demo_data.py
------------------
Generates realistic historical market data so the CMPIS dashboard behaves
exactly like a live system, without touching any external scraping API.

Design note for future replacement:
This module's ONLY job is to populate the `price_history` table. Everything
downstream (analytics.py, service.py, main.py) reads exclusively from the
database and has no idea whether the rows came from this simulator or from
a real scraper. Swapping in a live scraper later means writing a new module
that inserts PriceHistory rows in this same shape — zero changes needed
anywhere else in the codebase.

Simulation model per (product, vendor, day):
    price = base_price
            * monthly_trend_factor(day)   # slow ~90-day oscillation, ±3%
            * weekly_seasonality(day)     # day-of-week effect, ±1%
            * vendor_multiplier           # consistent per-vendor pricing strategy
            * daily_noise                 # small random volatility, ~0.5% std dev

This produces price curves with genuine trend + seasonality + noise
structure, rather than flat lines or pure random walks.
"""

from __future__ import annotations

import math
import random
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from live_scraper.database import AsyncSessionLocal, init_db
from demo_data.models import Product, PriceHistory

logger = logging.getLogger("cmpis.seed")

HISTORY_DAYS = 180

# ---------------------------------------------------------------------------
# Vendor pricing strategy — each vendor consistently prices relative to the
# "true" market base price. This is what makes vendor comparison meaningful
# instead of pure noise (e.g. BuildersMart is reliably the priciest).
# ---------------------------------------------------------------------------
VENDOR_MULTIPLIERS = {
    "IndustryBuying": 1.01,
    "IndiaMART": 0.99,
    "Moglix": 1.02,
    "BuildSupply": 1.00,
    "BuildersMart": 1.03,
}

VENDOR_SELLER_NAMES = {
    "IndustryBuying": "IndustryBuying Pvt Ltd",
    "IndiaMART": "IndiaMART Verified Seller",
    "Moglix": "Moglix Trading Co",
    "BuildSupply": "BuildSupply Direct",
    "BuildersMart": "BuildersMart Wholesale",
}

# ---------------------------------------------------------------------------
# Product catalog: (name, brand, category, unit, base_price_inr)
# Base prices are realistic ballpark figures for the Indian construction
# materials market and serve only as simulation anchors.
# ---------------------------------------------------------------------------
PRODUCT_CATALOG = [
    ("UltraTech Cement 50kg", "UltraTech", "Cement", "50kg bag", 380.0),
    ("ACC Cement 50kg", "ACC", "Cement", "50kg bag", 375.0),
    ("Ambuja Cement 50kg", "Ambuja", "Cement", "50kg bag", 370.0),
    ("JSW Cement 50kg", "JSW", "Cement", "50kg bag", 365.0),
    ("TMT Bar 8mm", "Generic", "Steel", "per kg", 62.0),
    ("TMT Bar 10mm", "Generic", "Steel", "per kg", 61.0),
    ("TMT Bar 12mm", "Generic", "Steel", "per kg", 60.0),
    ("TMT Bar 16mm", "Generic", "Steel", "per kg", 60.0),
    ("TMT Bar 20mm", "Generic", "Steel", "per kg", 60.0),
    ("River Sand", "Local Supplier", "Aggregates", "per ton", 1800.0),
    ("M Sand", "Local Supplier", "Aggregates", "per ton", 1500.0),
    ("Fly Ash Bricks", "Generic", "Bricks & Blocks", "per piece", 8.0),
    ("Concrete Blocks", "Generic", "Bricks & Blocks", "per piece", 45.0),
    ("Red Bricks", "Generic", "Bricks & Blocks", "per piece", 9.0),
    ("Steel Binding Wire", "Generic", "Steel", "per kg", 75.0),
    ("PVC Pipe 110mm", "Generic", "Plumbing", "per meter", 250.0),
    ("CPVC Pipe", "Generic", "Plumbing", "per meter", 180.0),
    ("UPVC Pipe", "Generic", "Plumbing", "per meter", 200.0),
    ("Wall Putty", "Generic", "Paints & Putty", "20kg bag", 850.0),
    ("Asian Paints Tractor Emulsion", "Asian Paints", "Paints & Putty", "per liter", 210.0),
]


def _monthly_trend_factor(day_index: int) -> float:
    """Slow oscillation over ~90 days, amplitude 3%, simulating market cycles."""
    return 1.0 + 0.03 * math.sin(2 * math.pi * day_index / 90.0)


def _weekly_seasonality_factor(day_index: int) -> float:
    """Mild day-of-week pricing effect, amplitude 1%."""
    return 1.0 + 0.01 * math.cos(2 * math.pi * (day_index % 7) / 7.0)


def _daily_noise_factor(rng: random.Random) -> float:
    """Small random volatility, ~0.5% standard deviation, gaussian distributed."""
    return 1.0 + rng.gauss(0, 0.005)


async def _generate_price_rows_for_product(
    product: Product, anchor_date: datetime
) -> List[PriceHistory]:
    """
    Build HISTORY_DAYS worth of price rows for one product across all
    vendors. `anchor_date` is treated as "today" — day_index=0 is the
    oldest day, day_index=HISTORY_DAYS-1 is today.
    """
    # Seed the RNG deterministically per-product so demo data is stable
    # across re-seeds (nice for reproducible dashboards/screenshots) while
    # still varying independently per product and per vendor draw.
    rng = random.Random(f"cmpis-seed-{product.name}")

    rows: List[PriceHistory] = []
    oldest_date = anchor_date - timedelta(days=HISTORY_DAYS - 1)

    for day_index in range(HISTORY_DAYS):
        current_date = oldest_date + timedelta(days=day_index)
        # Fix the collection time to midday so "collected_at.date()" is
        # unambiguous and consistent across all rows for that day.
        collected_at = current_date.replace(hour=12, minute=0, second=0, microsecond=0)

        trend = _monthly_trend_factor(day_index)
        season = _weekly_seasonality_factor(day_index)

        for vendor, multiplier in VENDOR_MULTIPLIERS.items():
            noise = _daily_noise_factor(rng)
            price = product.base_price * trend * season * multiplier * noise  # type: ignore[attr-defined]
            price = round(price, 2)

            rows.append(
                PriceHistory(
                    product_id=product.id,
                    source=vendor,
                    seller=VENDOR_SELLER_NAMES[vendor],
                    price=price,
                    currency="INR",
                    purchase_link=f"https://example.com/{vendor.lower()}/{product.id}",
                    availability="In Stock",
                    collected_at=collected_at,
                )
            )

    return rows


async def seed_all(session: AsyncSession) -> None:
    """
    Populate the products table and 180 days of price_history for every
    product in PRODUCT_CATALOG, using the given session.

    Idempotent guard: if products already exist, this is a no-op — call
    site (main.py) is responsible for checking emptiness first, but we
    double-check here too for safety when this module is run standalone.
    """
    existing_count_result = await session.execute(select(func.count()).select_from(Product))
    existing_count = existing_count_result.scalar_one()

    if existing_count > 0:
        logger.info("Products table already has %d rows — skipping seed.", existing_count)
        return

    anchor_date = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC "today"
    total_price_rows = 0

    for name, brand, category, unit, base_price in PRODUCT_CATALOG:
        try:
            product = Product(name=name, brand=brand, category=category, unit=unit)
            # Stash base_price as a transient attribute (not a mapped column)
            # purely so _generate_price_rows_for_product can read it below.
            product.base_price = base_price  # type: ignore[attr-defined]

            session.add(product)
            await session.flush()  # assigns product.id without committing yet

            price_rows = await _generate_price_rows_for_product(product, anchor_date)
            session.add_all(price_rows)
            total_price_rows += len(price_rows)

            logger.info("Seeded product %r with %d price rows.", name, len(price_rows))
        except Exception:
            logger.exception("Failed to seed product %r — skipping it.", name)
            continue

    await session.commit()
    logger.info(
        "Demo data seed complete: %d products, %d price_history rows.",
        len(PRODUCT_CATALOG), total_price_rows,
    )


async def main() -> None:
    """Standalone CLI entrypoint: bootstrap schema, seed, done."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    await init_db()
    async with AsyncSessionLocal() as session:
        try:
            await seed_all(session)
        except Exception:
            await session.rollback()
            raise


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())