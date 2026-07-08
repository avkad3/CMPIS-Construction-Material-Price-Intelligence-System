"""
analytics.py
------------
Pure business-logic layer: given a product name and a DB session, compute
the full suite of market intelligence metrics. Framework-agnostic — no
FastAPI imports here, so this logic is independently unit-testable and
reusable regardless of what sits on top of it.
"""

from __future__ import annotations

import statistics
import logging
from datetime import datetime, timedelta, date as date_type
from typing import List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Product, PriceHistory

logger = logging.getLogger("cmpis.analytics")

# Percentage band that separates GREEN / YELLOW / RED recommendations.
STATUS_THRESHOLD_PCT = 2.5


class MarketSummary(BaseModel):
    """Full analytics payload returned by /summary/{product_name}."""

    product_name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None

    today_date: date_type

    todays_lowest_price: float
    todays_highest_price: float
    todays_average_price: float

    historical_average_price: float
    historical_min_price: float
    historical_max_price: float

    cheapest_vendor_today: str
    most_expensive_vendor_today: str

    market_trend: str = Field(..., description="RISING | FALLING | STABLE | INSUFFICIENT_DATA")
    percentage_difference_from_average: float
    price_volatility_pct: float = Field(..., description="Coefficient of variation, as a percentage.")
    price_standard_deviation: float
    price_change_30d_pct: Optional[float] = Field(
        None, description="Percent change in average price vs. ~30 days ago, if enough history exists."
    )

    status: str = Field(..., description="GREEN | YELLOW | RED")
    recommendation: str

    data_points_considered: int
    todays_data_source: str = Field(
        ..., description="DEMO | LIVE | MIXED — origin of today's price rows."
    )


async def get_product_by_name(session: AsyncSession, product_name: str) -> Product:
    """Case-insensitive exact lookup. Raises LookupError if not found."""
    stmt = select(Product).where(func.lower(Product.name) == product_name.lower())
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()
    if product is None:
        raise LookupError(f"Product '{product_name}' not found in catalog.")
    return product


async def get_history_rows(session: AsyncSession, product_id: int) -> List[PriceHistory]:
    """All price_history rows for a product, ordered chronologically."""
    stmt = (
        select(PriceHistory)
        .where(PriceHistory.product_id == product_id)
        .order_by(PriceHistory.collected_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def daily_average_series(rows: List[PriceHistory]) -> List[tuple[date_type, float]]:
    """
    Collapse potentially-multiple vendor rows per day into one (date, avg_price)
    point per day, sorted ascending by date. Used for trend/volatility/30-day
    change calculations so multi-vendor noise doesn't distort the time series.
    """
    by_date: dict[date_type, List[float]] = {}
    for row in rows:
        by_date.setdefault(row.collected_date, []).append(row.price)

    return sorted(
        ((d, sum(prices) / len(prices)) for d, prices in by_date.items()),
        key=lambda pair: pair[0],
    )


def _compute_status(deviation_pct: float) -> tuple[str, str]:
    """
    Map a percentage deviation from historical average to a status code
    and human recommendation, per the CMPIS thresholds.
    """
    if deviation_pct < -STATUS_THRESHOLD_PCT:
        return "GREEN", "Buy Now"
    elif deviation_pct > STATUS_THRESHOLD_PCT:
        return "RED", "Wait if possible"
    else:
        return "YELLOW", "Fair Market Price"


def _compute_trend(daily_series: List[tuple[date_type, float]]) -> str:
    """
    Compare the average of the most recent 7 days against the prior 7 days
    to classify the short-term trend direction.
    """
    if len(daily_series) < 14:
        return "INSUFFICIENT_DATA"

    recent_7 = [price for _, price in daily_series[-7:]]
    prior_7 = [price for _, price in daily_series[-14:-7]]

    recent_avg = sum(recent_7) / len(recent_7)
    prior_avg = sum(prior_7) / len(prior_7)

    if prior_avg == 0:
        return "INSUFFICIENT_DATA"

    change_pct = ((recent_avg - prior_avg) / prior_avg) * 100.0
    if change_pct > 0.5:
        return "RISING"
    elif change_pct < -0.5:
        return "FALLING"
    else:
        return "STABLE"


def _compute_30d_change(daily_series: List[tuple[date_type, float]]) -> Optional[float]:
    """
    Percentage change in average daily price between today and ~30 days ago.
    Returns None if there isn't enough history to make the comparison meaningful.
    """
    if len(daily_series) < 31:
        return None

    today_price = daily_series[-1][1]
    price_30d_ago = daily_series[-31][1]  # 30 days back from today's index

    if price_30d_ago == 0:
        return None

    return round(((today_price - price_30d_ago) / price_30d_ago) * 100.0, 2)


async def compute_market_summary(session: AsyncSession, product_name: str) -> MarketSummary:
    """
    Core analytics routine. Raises LookupError if the product doesn't exist
    or has no price history yet — callers (service.py/main.py) translate
    that into a clean 404 rather than a 500.
    """
    product = await get_product_by_name(session, product_name)
    rows = await get_history_rows(session, product.id)

    if not rows:
        raise LookupError(f"No price history captured yet for '{product_name}'.")

    all_prices = [row.price for row in rows]
    historical_average = sum(all_prices) / len(all_prices)
    historical_min = min(all_prices)
    historical_max = max(all_prices)

    # "Today" = the most recent date actually present in the data, so the
    # analytics remain correct even if the seed/scrape hasn't run yet today.
    latest_date = max(row.collected_date for row in rows)
    todays_rows = [row for row in rows if row.collected_date == latest_date]

    todays_prices = [row.price for row in todays_rows]
    todays_lowest = min(todays_prices)
    todays_highest = max(todays_prices)
    todays_average = sum(todays_prices) / len(todays_prices)

    cheapest_row = min(todays_rows, key=lambda r: r.price)
    priciest_row = max(todays_rows, key=lambda r: r.price)

    # Standard deviation needs at least 2 data points; guard against a
    # single-vendor edge case where stdev is undefined.
    price_std_dev = statistics.pstdev(all_prices) if len(all_prices) > 1 else 0.0
    volatility_pct = (price_std_dev / historical_average * 100.0) if historical_average > 0 else 0.0

    deviation_pct = (
        ((todays_lowest - historical_average) / historical_average) * 100.0
        if historical_average > 0
        else 0.0
    )
    status, recommendation = _compute_status(deviation_pct)

    daily_series = daily_average_series(rows)
    trend = _compute_trend(daily_series)
    change_30d = _compute_30d_change(daily_series)

    # Determine whether today's rows came from the demo simulator, a live
    # scrape, or a mix of both (e.g. user ran both on the same day).
    todays_sources = {row.data_source for row in todays_rows}
    if todays_sources == {"demo"}:
        todays_data_source = "DEMO"
    elif todays_sources == {"live"}:
        todays_data_source = "LIVE"
    else:
        todays_data_source = "MIXED"

    return MarketSummary(
        product_name=product.name,
        brand=product.brand,
        category=product.category,
        unit=product.unit,
        today_date=latest_date,
        todays_lowest_price=round(todays_lowest, 2),
        todays_highest_price=round(todays_highest, 2),
        todays_average_price=round(todays_average, 2),
        historical_average_price=round(historical_average, 2),
        historical_min_price=round(historical_min, 2),
        historical_max_price=round(historical_max, 2),
        cheapest_vendor_today=cheapest_row.source,
        most_expensive_vendor_today=priciest_row.source,
        market_trend=trend,
        percentage_difference_from_average=round(deviation_pct, 2),
        price_volatility_pct=round(volatility_pct, 2),
        price_standard_deviation=round(price_std_dev, 2),
        price_change_30d_pct=change_30d,
        status=status,
        recommendation=recommendation,
        data_points_considered=len(rows),
        todays_data_source=todays_data_source,
    )