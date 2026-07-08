"""
main.py
-------
FastAPI application entrypoint for CMPIS (demo-data backend).

On startup:
  1. Initialize the database schema (create tables if missing).
  2. Seed 180 days of realistic demo market data — but ONLY if the
     products table is currently empty, so restarts don't re-seed or
     duplicate data.

All routes are thin wrappers around service.py — no business logic lives
in this file, only HTTP concerns (status codes, request/response shaping).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from demo_data.database import init_db, get_db, dispose_engine, AsyncSessionLocal
from demo_data.models import Product
import demo_data.service
from demo_data.analytics import MarketSummary
import demo_data.seed_demo_data
import demo_data.po_service
from demo_data.po_service import POAnalyzeRequest, POAnalysisResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cmpis.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Modern FastAPI startup/shutdown handling (replaces the deprecated
    @app.on_event decorators). Initializes schema, seeds demo data if
    needed, and disposes the connection pool cleanly on shutdown.
    """
    try:
        await init_db()

        # Only seed if the products table is currently empty — makes
        # repeated app restarts safe and non-destructive.
        async with AsyncSessionLocal() as session:
            count_result = await session.execute(select(func.count()).select_from(Product))
            product_count = count_result.scalar_one()

            if product_count == 0:
                logger.info("Products table is empty — seeding demo data.")
                await demo_data.seed_demo_data.seed_all(session)
            else:
                logger.info("Products table already has %d rows — skipping seed.", product_count)
    except Exception:
        logger.exception("Startup initialization failed.")
        raise

    yield  # application runs here

    await dispose_engine()


app = FastAPI(
    title="CMPIS Backend",
    description="Construction Material Price Intelligence System — demo-data backend.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["ops"])
async def health_check() -> dict:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/products", tags=["catalog"])
async def list_products(db: AsyncSession = Depends(get_db)) -> dict:
    """Return every tracked product in the catalog."""
    try:
        products = await demo_data.service.list_all_products(db)
        return {"count": len(products), "products": products}
    except Exception:
        logger.exception("Failed to list products.")
        raise HTTPException(status_code=500, detail="Internal server error while listing products.")


@app.get("/search/{product_name}", tags=["catalog"])
async def search_product(
    product_name: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Partial, case-insensitive product name search."""
    try:
        results = await demo_data.service.search_products(db, product_name)
        return {"query": product_name, "count": len(results), "results": results}
    except Exception:
        logger.exception("Search failed for query '%s'.", product_name)
        raise HTTPException(status_code=500, detail="Internal server error during product search.")


@app.get("/history/{product_name}", tags=["analytics"])
async def get_price_history(
    product_name: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Daily-average price history formatted for direct use with React Recharts."""
    try:
        return await demo_data.service.get_price_history_chart(db, product_name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        logger.exception("Failed to fetch price history for '%s'.", product_name)
        raise HTTPException(status_code=500, detail="Internal server error while fetching price history.")


@app.get("/summary/{product_name}", response_model=MarketSummary, tags=["analytics"])
async def get_market_summary(
    product_name: str, db: AsyncSession = Depends(get_db)
) -> MarketSummary:
    """Full market intelligence summary: today's stats, historical stats, trend, status, recommendation."""
    try:
        return await demo_data.service.get_market_summary(db, product_name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Failed to compute market summary for '%s'.", product_name)
        raise HTTPException(status_code=500, detail="Internal server error while computing market summary.")


@app.get("/vendors/{product_name}", tags=["analytics"])
async def get_vendor_comparison(
    product_name: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Per-vendor price comparison: today's price + historical average, cheapest first."""
    try:
        return await demo_data.service.get_vendor_comparison(db, product_name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        logger.exception("Failed to compute vendor comparison for '%s'.", product_name)
        raise HTTPException(status_code=500, detail="Internal server error while computing vendor comparison.")


@app.post("/po/analyze", response_model=POAnalysisResponse, tags=["purchase-orders"])
async def analyze_purchase_order(
    request: POAnalyzeRequest, db: AsyncSession = Depends(get_db)
) -> POAnalysisResponse:
    """
    Accepts a parsed list of PO line items (raw product description + quantity),
    fuzzy-matches each against the product catalog, and returns full price
    intelligence + estimated cost for every matched line item. Unmatched items
    are returned separately with best-guess suggestions rather than failing
    the whole request.
    """
    try:
        return await demo_data.po_service.analyze_po(db, request.items)
    except Exception:
        logger.exception("Failed to analyze purchase order.")
        raise HTTPException(status_code=500, detail="Internal server error while analyzing purchase order.")


# ---------------------------------------------------------------------------
# Admin / data-source management
# ---------------------------------------------------------------------------
class ReseedRequest(BaseModel):
    force: bool = False


@app.get("/admin/data-status", tags=["admin"])
async def data_status(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Reports current database state (product count, demo vs. live row counts,
    most recent collection timestamp) so the UI can show what's loaded
    before the user decides to seed, scrape, or reset.
    """
    try:
        return await demo_data.service.get_data_status(db)
    except Exception:
        logger.exception("Failed to fetch data status.")
        raise HTTPException(status_code=500, detail="Internal server error while fetching data status.")


@app.post("/admin/seed-demo", tags=["admin"])
async def seed_demo(request: ReseedRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """
    Trigger the demo data generator. By default this is a no-op if products
    already exist — pass {"force": true} to wipe and regenerate anyway.
    """
    try:
        return await demo_data.service.run_demo_seed(db, force=request.force)
    except Exception:
        logger.exception("Failed to seed demo data.")
        raise HTTPException(status_code=500, detail="Internal server error while seeding demo data.")


@app.post("/admin/scrape-live", tags=["admin"])
async def scrape_live(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Trigger an on-demand live scrape via Serper.dev. Only runs when the
    user explicitly asks for it (this button/endpoint) — never on app
    startup — so a slow or unavailable third-party API can never block
    the app from booting or affect any other request.
    """
    try:
        return await demo_data.service.run_live_scrape(db)
    except RuntimeError as exc:
        # e.g. SERPER_API_KEY not configured — a config problem, not a server crash.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("Live scrape failed.")
        raise HTTPException(status_code=500, detail="Internal server error while running the live scraper.")


@app.post("/admin/reset", tags=["admin"])
async def reset_data(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Wipe every product and price_history row so the user can start
    completely fresh with either data source. Irreversible — the frontend
    should confirm with the user before calling this.
    """
    try:
        return await demo_data.service.reset_all_data(db)
    except Exception:
        logger.exception("Failed to reset data.")
        raise HTTPException(status_code=500, detail="Internal server error while resetting data.")