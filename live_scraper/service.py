"""
service.py
----------
FastAPI Price Analytics Engine for CMPIS.

CRITICAL ARCHITECTURAL PROPERTY:
This layer NEVER calls Serper.dev or performs any live network scraping.
It exclusively reads pre-scraped data written by worker.py from Postgres.
This is what guarantees user search requests are fast, predictable, and
immune to third-party scraping timeouts — the decoupling the system
was designed around.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from demo_data.models import Product, PriceHistory
from live_scraper.database import get_session, init_db, dispose_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cmpis.service")

# Percentage threshold that separates GREEN / YELLOW / RED status bands.
STATUS_THRESHOLD_PCT = 2.5


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------
class MerchantOffer(BaseModel):
    source: str
    price: float
    purchase_link: Optional[str] = None


class PriceIntelligenceResponse(BaseModel):
    product_name: str
    category: Optional[str] = None
    todays_lowest_price: float = Field(..., description="Cheapest price found today across all vendors.")
    average_market_price: float = Field(..., description="Average price across the full captured history.")
    status: str = Field(..., description="GREEN | YELLOW | RED market status indicator.")
    percentage_deviation: float = Field(..., description="Signed % deviation of today's low vs. the average.")
    todays_offers: List[MerchantOffer] = Field(default_factory=list)
    data_points_considered: int = Field(..., description="Total historical price_history rows used in the average.")


# ---------------------------------------------------------------------------
# Business logic (pure, testable, framework-agnostic)
# ---------------------------------------------------------------------------
def compute_status(todays_price: float, average_price: float) -> tuple[str, float]:
    """
    Determine the market status color code and percentage deviation.

    GREEN  -> today's price is more than STATUS_THRESHOLD_PCT % BELOW average (a good deal)
    RED    -> today's price is more than STATUS_THRESHOLD_PCT % ABOVE average (overpriced)
    YELLOW -> today's price sits within the fair +/- threshold band around average

    Returns a tuple of (status_code, signed_percentage_deviation).
    """
    if average_price <= 0:
        # Defensive guard: avoid division by zero on corrupt/empty data.
        raise ValueError("Average price must be greater than zero to compute deviation.")

    deviation_pct = ((todays_price - average_price) / average_price) * 100.0

    if deviation_pct < -STATUS_THRESHOLD_PCT:
        status = "GREEN"
    elif deviation_pct > STATUS_THRESHOLD_PCT:
        status = "RED"
    else:
        status = "YELLOW"

    return status, round(deviation_pct, 2)


async def get_price_intelligence(
    session: AsyncSession, product_name: str
) -> PriceIntelligenceResponse:
    """
    Core analytics routine:
      1. Locate the product by exact name.
      2. Pull its full price_history.
      3. Compute today's lowest price, historical average, and status band.

    Raises:
        LookupError: if the product doesn't exist or has no price history yet.
    """
    # Step 1: locate the product (case-insensitive exact match for robustness).
    product_stmt = select(Product).where(func.lower(Product.name) == product_name.lower())
    product_result = await session.execute(product_stmt)
    product = product_result.scalar_one_or_none()

    if product is None:
        raise LookupError(f"Product '{product_name}' not found in catalog.")

    # Step 2: pull ALL historical price rows for this product.
    history_stmt = select(PriceHistory).where(PriceHistory.product_id == product.id)
    history_result = await session.execute(history_stmt)
    history_rows: List[PriceHistory] = list(history_result.scalars().all())

    if not history_rows:
        raise LookupError(f"No price history captured yet for '{product_name}'.")

    # Step 3a: average market price across the ENTIRE captured timeline.
    average_price = sum(row.price for row in history_rows) / len(history_rows)

    # Step 3b: today's snapshot — filter to rows dated today.
    today = date.today()
    todays_rows = [row for row in history_rows if row.collected_date == today]

    if todays_rows:
        lowest_today_row = min(todays_rows, key=lambda r: r.price)
        todays_lowest_price = lowest_today_row.price
        todays_offers = [
            MerchantOffer(source=r.source, price=r.price, purchase_link=r.purchase_link)
            for r in sorted(todays_rows, key=lambda r: r.price)
        ]
    else:
        # Fallback: if today's scrape hasn't landed yet (e.g. worker hasn't
        # run yet this cycle), gracefully fall back to the most recent
        # available date rather than erroring out on the user.
        logger.warning(
            "No price_history rows for today (%s) on product '%s' — falling back to latest date.",
            today, product_name,
        )
        latest_date = max(row.collected_date for row in history_rows)
        latest_rows = [row for row in history_rows if row.collected_date == latest_date]
        lowest_today_row = min(latest_rows, key=lambda r: r.price)
        todays_lowest_price = lowest_today_row.price
        todays_offers = [
            MerchantOffer(source=r.source, price=r.price, purchase_link=r.purchase_link)
            for r in sorted(latest_rows, key=lambda r: r.price)
        ]

    status, deviation_pct = compute_status(todays_lowest_price, average_price)

    return PriceIntelligenceResponse(
        product_name=product.name,
        category=product.category,
        todays_lowest_price=round(todays_lowest_price, 2),
        average_market_price=round(average_price, 2),
        status=status,
        percentage_deviation=deviation_pct,
        todays_offers=todays_offers,
        data_points_considered=len(history_rows),
    )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="CMPIS Price Analytics Engine",
    description="Business-logic layer for construction material market intelligence.",
    version="1.0.0",
)


@app.on_event("startup")
async def on_startup() -> None:
    """Verify schema exists on boot (safe no-op if already created by worker.py)."""
    try:
        await init_db()
    except Exception:
        logger.exception("Startup schema verification failed.")
        raise


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Cleanly release the DB connection pool when the app stops."""
    await dispose_engine()


@app.get("/health", tags=["ops"])
async def health_check() -> dict:
    """Simple liveness probe endpoint."""
    return {"status": "ok"}


@app.get(
    "/api/v1/price-intelligence",
    response_model=PriceIntelligenceResponse,
    tags=["analytics"],
    summary="Get market intelligence (lowest price, average, status) for a product.",
)
async def price_intelligence_endpoint(
    product_name: str = Query(..., min_length=2, description="Exact or near-exact product name."),
) -> PriceIntelligenceResponse:
    """
    Public endpoint consumed by the frontend/search UI.

    Note: this endpoint is backed ENTIRELY by pre-scraped database rows.
    It never triggers a live scrape, so response times stay fast and
    predictable regardless of Serper.dev's availability or latency.
    """
    try:
        async with get_session() as session:
            return await get_price_intelligence(session, product_name)
    except LookupError as exc:
        # Expected "not found" case -> clean 404, not a 500.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # e.g. corrupt zero-average data -> 422 unprocessable rather than crash.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Unexpected error computing price intelligence for '%s'.", product_name)
        raise HTTPException(status_code=500, detail="Internal server error while computing price intelligence.")