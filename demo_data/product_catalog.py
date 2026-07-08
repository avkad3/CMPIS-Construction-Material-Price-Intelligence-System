"""
product_catalog.py
-------------------
The single source of truth for which construction materials CMPIS tracks.

Both seed_demo_data.py (simulated history) and live_scraper.py (real
Serper.dev scraping) read from this same catalog, so switching data
sources never changes *which* products exist — only where their price
history comes from.
"""

from __future__ import annotations

import logging
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Product

logger = logging.getLogger("cmpis.catalog")

# (name, brand, category, unit, demo_base_price)
# demo_base_price is only used as a simulation anchor by seed_demo_data.py —
# live_scraper.py ignores it entirely and uses real scraped prices instead.
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


async def ensure_products_exist(session: AsyncSession) -> List[Product]:
    """
    Idempotently make sure every product in PRODUCT_CATALOG has a row in
    the `products` table. Safe to call from both the demo seeder and the
    live scraper — existing products are left untouched, only missing
    ones are inserted.

    Returns the full list of Product ORM objects (existing + newly created).
    """
    result = await session.execute(select(Product))
    existing_products = {p.name: p for p in result.scalars().all()}

    created_count = 0
    for name, brand, category, unit, _base_price in PRODUCT_CATALOG:
        if name not in existing_products:
            product = Product(name=name, brand=brand, category=category, unit=unit)
            session.add(product)
            existing_products[name] = product
            created_count += 1

    if created_count:
        await session.flush()  # assign IDs to newly created products
        logger.info("Created %d new product(s) from catalog.", created_count)

    return list(existing_products.values())