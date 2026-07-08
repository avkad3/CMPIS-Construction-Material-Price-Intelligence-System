"""
service.py
----------
Service layer: thin, async, framework-light business operations that sit
between main.py's HTTP routes and the database/analytics layers.

Kept separate from main.py so route wiring (HTTP concerns) never mixes
with actual business/query logic (testable independent of FastAPI).
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Product, PriceHistory
from .analytics import compute_market_summary, MarketSummary, get_product_by_name, daily_average_series, get_history_rows

logger = logging.getLogger("cmpis.service")


async def search_products(session: AsyncSession, query: str) -> List[Dict[str, Any]]:
    """
    Partial, case-insensitive product name search — powers a search-as-you-type
    frontend experience. Returns lightweight product summaries, not full analytics.
    """
    stmt = (
        select(Product)
        .where(func.lower(Product.name).contains(query.lower()))
        .order_by(Product.name.asc())
    )
    result = await session.execute(stmt)
    products = result.scalars().all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "brand": p.brand,
            "category": p.category,
            "unit": p.unit,
        }
        for p in products
    ]


async def list_all_products(session: AsyncSession) -> List[Dict[str, Any]]:
    """Return every tracked product — powers the initial catalog/browse view."""
    stmt = select(Product).order_by(Product.category.asc(), Product.name.asc())
    result = await session.execute(stmt)
    products = result.scalars().all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "brand": p.brand,
            "category": p.category,
            "unit": p.unit,
        }
        for p in products
    ]


async def get_price_history_chart(session: AsyncSession, product_name: str) -> Dict[str, Any]:
    """
    Returns daily-average price history formatted for direct use with
    React's Recharts library:
        [{"date": "2026-01-01", "price": 382.4}, ...]

    Raises LookupError if the product doesn't exist or has no history.
    """
    product = await get_product_by_name(session, product_name)
    rows = await get_history_rows(session, product.id)

    if not rows:
        raise LookupError(f"No price history captured yet for '{product_name}'.")

    daily_series = daily_average_series(rows)
    chart_data = [
        {"date": d.isoformat(), "price": round(price, 2)}
        for d, price in daily_series
    ]

    return {
        "product_name": product.name,
        "unit": product.unit,
        "data_points": len(chart_data),
        "history": chart_data,
    }


async def get_market_summary(session: AsyncSession, product_name: str) -> MarketSummary:
    """Thin passthrough to the analytics engine — kept here for a consistent service API surface."""
    return await compute_market_summary(session, product_name)


async def get_vendor_comparison(session: AsyncSession, product_name: str) -> Dict[str, Any]:
    """
    Per-vendor comparison for a product: today's price plus each vendor's
    historical average, sorted cheapest-first. Powers a vendor comparison
    table/chart on the frontend.
    """
    product = await get_product_by_name(session, product_name)
    rows = await get_history_rows(session, product.id)

    if not rows:
        raise LookupError(f"No price history captured yet for '{product_name}'.")

    latest_date = max(row.collected_date for row in rows)

    # Group all historical rows by vendor to compute each vendor's own average,
    # and separately capture each vendor's price on the most recent date.
    vendor_all_prices: Dict[str, List[float]] = {}
    vendor_today_price: Dict[str, float] = {}
    vendor_purchase_link: Dict[str, str] = {}
    vendor_data_source: Dict[str, str] = {}

    for row in rows:
        vendor_all_prices.setdefault(row.source, []).append(row.price)
        if row.collected_date == latest_date:
            vendor_today_price[row.source] = row.price
            vendor_purchase_link[row.source] = row.purchase_link or ""
            vendor_data_source[row.source] = row.data_source

    comparison = [
        {
            "vendor": vendor,
            "todays_price": vendor_today_price.get(vendor),
            "historical_average_price": round(sum(prices) / len(prices), 2),
            "purchase_link": vendor_purchase_link.get(vendor, ""),
            "data_source": vendor_data_source.get(vendor, "unknown"),
        }
        for vendor, prices in vendor_all_prices.items()
    ]

    # Cheapest vendor (by today's price) first; vendors with no data today
    # (edge case, e.g. mid-migration) sort to the end instead of crashing.
    comparison.sort(key=lambda v: (v["todays_price"] is None, v["todays_price"] or float("inf")))

    return {
        "product_name": product.name,
        "as_of_date": latest_date.isoformat(),
        "vendors": comparison,
    }


# ---------------------------------------------------------------------------
# Admin / data-source management
# ---------------------------------------------------------------------------
async def get_data_status(session: AsyncSession) -> Dict[str, Any]:
    """
    Reports the current state of the database so the UI can show the user
    what's loaded before they decide what action to take next.
    """
    from .models import PriceHistory  # local import to avoid polluting module-level namespace

    product_count_result = await session.execute(select(func.count()).select_from(Product))
    product_count = product_count_result.scalar_one()

    total_rows_result = await session.execute(select(func.count()).select_from(PriceHistory))
    total_rows = total_rows_result.scalar_one()

    demo_rows_result = await session.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.data_source == "demo")
    )
    demo_rows = demo_rows_result.scalar_one()

    live_rows_result = await session.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.data_source == "live")
    )
    live_rows = live_rows_result.scalar_one()

    latest_collected_result = await session.execute(select(func.max(PriceHistory.collected_at)))
    latest_collected = latest_collected_result.scalar_one()

    return {
        "product_count": product_count,
        "price_history_total": total_rows,
        "demo_rows": demo_rows,
        "live_rows": live_rows,
        "latest_collected_at": latest_collected.isoformat() if latest_collected else None,
        "has_any_data": total_rows > 0,
    }


async def run_demo_seed(session: AsyncSession, force: bool = False) -> Dict[str, Any]:
    """Trigger the demo data generator. Thin passthrough kept here for a consistent service API surface."""
    import seed_demo_data
    return await seed_demo_data.seed_all(session, force=force)


async def run_live_scrape(session: AsyncSession) -> Dict[str, Any]:
    """Trigger the live Serper.dev scraper. Raises RuntimeError if SERPER_API_KEY isn't configured."""
    import live_scraper
    return await live_scraper.run_live_scrape(session)


async def reset_all_data(session: AsyncSession) -> Dict[str, Any]:
    """
    Wipe every product and price_history row (cascades via FK), so the
    user can start completely fresh with either data source.
    """
    from .models import PriceHistory  # local import, consistent with get_data_status above
    from sqlalchemy import delete

    await session.execute(delete(PriceHistory))
    await session.execute(delete(Product))
    await session.commit()
    logger.info("All product and price_history data has been reset.")
    return {"status": "reset_complete"}