"""
worker.py
---------
Decoupled scraping pipeline for CMPIS.

This module is the ONLY part of the system that talks to the internet.
It runs as an independent, schedulable job (cron / Celery beat / APScheduler /
Kubernetes CronJob — your choice of orchestrator) and writes fresh price
snapshots into Postgres.

Because the FastAPI service layer (service.py) reads exclusively from the
database and never calls Serper directly, user-facing search requests are
fully decoupled from live scraping and can NEVER time out waiting on a
third-party API.

Run standalone with:  python worker.py
"""

from __future__ import annotations

import os
import re
import asyncio
import logging
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select, func

from demo_data.models import Product, PriceHistory
from live_scraper.database import get_session, init_db, dispose_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cmpis.worker")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERPER_API_URL = "https://google.serper.dev/shopping"
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")

# Safety limits so one bad run doesn't hammer the API or hang forever.
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_MERCHANTS_PER_PRODUCT = 5
MAX_CONCURRENT_REQUESTS = 5  # throttle to avoid rate-limiting / IP bans
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0

_PRICE_CLEAN_PATTERN = re.compile(r"[^\d.]")  # strip everything except digits and decimal point


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_price(raw_price: Any) -> Optional[float]:
    """
    Normalize a raw price string like '₹1,234.50' or 'Rs. 1,234' into a
    clean float. Returns None if the value cannot be parsed, so callers
    can safely skip malformed entries instead of crashing the whole batch.
    """
    if raw_price is None:
        return None
    try:
        if isinstance(raw_price, (int, float)):
            return float(raw_price)

        text = str(raw_price).strip()
        # Strip currency symbols (₹, Rs, INR), commas, and any stray whitespace.
        text = text.replace("₹", "").replace(",", "")
        text = re.sub(r"(?i)\brs\.?\b|\binr\b", "", text)
        text = _PRICE_CLEAN_PATTERN.sub("", text).strip()

        if not text:
            return None
        return float(text)
    except (ValueError, TypeError):
        logger.warning("Could not parse price value: %r", raw_price)
        return None


def _build_payload(product_name: str) -> Dict[str, Any]:
    """Standard Serper.dev Shopping API payload targeting the India region."""
    return {
        "q": product_name,
        "gl": "in",
        "hl": "en",
    }


async def _fetch_shopping_results(
    client: httpx.AsyncClient, product_name: str
) -> Optional[Dict[str, Any]]:
    """
    Call the Serper.dev Shopping API for a single product with retry/backoff.
    Returns the parsed JSON body, or None if every attempt fails — a failure
    here should never crash the whole batch job.
    """
    if not SERPER_API_KEY:
        logger.error("SERPER_API_KEY is not set. Aborting fetch for %r.", product_name)
        return None

    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = _build_payload(product_name)

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await client.post(
                SERPER_API_URL,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            logger.warning(
                "Timeout fetching %r (attempt %d/%d).", product_name, attempt, RETRY_ATTEMPTS
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HTTP error %s fetching %r (attempt %d/%d).",
                exc.response.status_code, product_name, attempt, RETRY_ATTEMPTS,
            )
            # Don't retry on 4xx client errors (bad request/auth) — won't self-resolve.
            if 400 <= exc.response.status_code < 500:
                break
        except (httpx.RequestError, ValueError) as exc:
            logger.warning(
                "Request/parse error fetching %r (attempt %d/%d): %s",
                product_name, attempt, RETRY_ATTEMPTS, exc,
            )

        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)  # simple exponential backoff

    logger.error("Exhausted all retries fetching shopping results for %r.", product_name)
    return None


async def _process_product(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    product_id: int,
    product_name: str,
) -> List[PriceHistory]:
    """
    Fetch + parse the top merchant matches for a single product and return
    a list of ready-to-persist PriceHistory ORM objects (not yet committed).
    """
    async with semaphore:  # throttle concurrent outbound requests
        raw_data = await _fetch_shopping_results(client, product_name)

    if not raw_data:
        return []

    shopping_results = raw_data.get("shopping", []) or []
    today = date.today()
    collected_at = datetime.combine(today, time(hour=12, minute=0, second=0))
    entries: List[PriceHistory] = []
    seen_sources: set[str] = set()  # tracks sources already added THIS run for THIS product

    for item in shopping_results[:MAX_MERCHANTS_PER_PRODUCT]:
        source = item.get("source") or item.get("seller") or "Unknown"
        purchase_link = item.get("link")
        price_value = clean_price(item.get("price"))

        if price_value is None:
            logger.warning(
                "Skipping unparsable price for product=%r source=%r raw=%r",
                product_name, source, item.get("price"),
            )
            continue

        # Serper's shopping results can list the same merchant more than
        # once (e.g. different listings/variants from the same seller).
        # Since our schema allows only ONE (product, source, date) row,
        # we keep just the first — typically the highest-ranked/cheapest
        # — occurrence and skip subsequent duplicates from this same batch.
        if source in seen_sources:
            logger.info(
                "Skipping duplicate merchant %r for product=%r within same batch.",
                source, product_name,
            )
            continue
        seen_sources.add(source)

        entries.append(
            PriceHistory(
                product_id=product_id,
                source=source,
                price=price_value,
                collected_at=collected_at,
                purchase_link=purchase_link,
            )
        )

    logger.info(
        "Product %r: parsed %d/%d valid merchant entries.",
        product_name, len(entries), len(shopping_results[:MAX_MERCHANTS_PER_PRODUCT]),
    )
    return entries


# ---------------------------------------------------------------------------
# Main pipeline entrypoint
# ---------------------------------------------------------------------------
async def run_price_scraping_pipeline() -> None:
    """
    Full batch job:
      1. Load all tracked products from Postgres.
      2. Concurrently query Serper.dev Shopping for each (throttled).
      3. Clean + persist today's price snapshots into price_history.

    This function is designed to be invoked by an external scheduler
    (cron, Celery beat, APScheduler, Airflow, K8s CronJob, etc.) on a
    daily/hourly cadence — completely independent of user search traffic.
    """
    logger.info("Starting CMPIS price scraping pipeline run.")

    try:
        async with get_session() as session:
            result = await session.execute(select(Product))
            products = result.scalars().all()
    except Exception:
        logger.exception("Failed to load product list from database. Aborting run.")
        return

    if not products:
        logger.info("No products found to scrape. Exiting pipeline early.")
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    all_new_entries: List[PriceHistory] = []

    try:
        async with httpx.AsyncClient() as client:
            tasks = [
                _process_product(client, semaphore, product.id, product.name)
                for product in products
            ]
            # return_exceptions=True ensures ONE failing product never
            # aborts the entire batch for every other product.
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for product, outcome in zip(products, results):
            if isinstance(outcome, Exception):
                logger.error("Unhandled error scraping product %r: %s", product.name, outcome)
                continue
            all_new_entries.extend(outcome)

    except Exception:
        logger.exception("Unexpected failure during concurrent scraping phase.")
        return

    if not all_new_entries:
        logger.warning("Pipeline run completed but produced zero new price entries.")
        return

    # ------------------------------------------------------------------
    # De-duplicate against rows that already exist for TODAY before
    # inserting. Without this, re-running the worker on the same day
    # (e.g. manual retry, scheduler overlap, restart after a crash)
    # would violate the (product_id, source, date) UniqueConstraint and
    # roll back the ENTIRE batch — including legitimately new rows for
    # other products. This makes the pipeline safely idempotent.
    # ------------------------------------------------------------------
    today = date.today()
    try:
        async with get_session() as session:
            existing_stmt = select(
                PriceHistory.product_id, PriceHistory.source
            ).where(func.date(PriceHistory.collected_at) == today.isoformat())
            existing_result = await session.execute(existing_stmt)
            existing_keys = set(existing_result.all())  # set of (product_id, source) tuples
    except Exception:
        logger.exception("Failed to load existing price_history keys for dedupe check. Aborting persist.")
        return

    deduped_entries = [
        entry for entry in all_new_entries
        if (entry.product_id, entry.source) not in existing_keys
    ]
    skipped_count = len(all_new_entries) - len(deduped_entries)
    if skipped_count:
        logger.info(
            "Skipped %d duplicate entries already captured today (idempotent re-run).",
            skipped_count,
        )

    if not deduped_entries:
        logger.info("No new (non-duplicate) price entries to persist for today.")
        return

    # Persist everything in a single transaction for atomicity/performance.
    try:
        async with get_session() as session:
            session.add_all(deduped_entries)
            # commit happens automatically inside get_session()'s __aexit__
        logger.info(
            "Pipeline run complete. Persisted %d new price_history rows across %d products.",
            len(deduped_entries), len(products),
        )
    except Exception:
        logger.exception("Failed to persist scraped price entries to database.")


async def main() -> None:
    """Standalone CLI entrypoint: bootstrap schema, run once, clean up."""
    try:
        await init_db()
        await run_price_scraping_pipeline()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())