"""
po_service.py
-------------
Purchase Order (PO) analysis for CMPIS.

Business flow:
  1. Caller supplies a list of raw PO line items (free-text product
     description + quantity) — parsing of uploaded files/pasted text
     happens in the frontend; this module only deals with already-split
     (raw_text, quantity) pairs.
  2. Each raw_text is fuzzy-matched against the product catalog, since a
     real PO will rarely spell a product name exactly as it exists in
     our `products` table (e.g. "Ultratech cement 50kg bag" vs
     "UltraTech Cement 50kg").
  3. Matched items get a full price-intelligence lookup (reusing
     analytics.compute_market_summary — no duplicated analytics logic)
     and an estimated line total (current price x quantity).
  4. Unmatched items are returned separately with best-guess suggestions
     so the user can correct typos or add missing products.
"""

from __future__ import annotations

import difflib
import logging
from typing import List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Product
from .analytics import compute_market_summary

logger = logging.getLogger("cmpis.po_service")

# Minimum similarity ratio (0-1) for a fuzzy match to be accepted at all.
MATCH_ACCEPT_THRESHOLD = 0.55
# Lower threshold used only to generate "did you mean?" suggestions for
# items that didn't clear the acceptance bar.
SUGGESTION_THRESHOLD = 0.35
MAX_SUGGESTIONS = 3


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class POLineItem(BaseModel):
    """One line item as extracted from the PO, before matching."""
    raw_text: str = Field(..., description="Free-text product description as written in the PO.")
    quantity: float = Field(1.0, gt=0, description="Quantity ordered. Defaults to 1 if not specified.")


class POAnalyzeRequest(BaseModel):
    items: List[POLineItem]


class MatchedPOItem(BaseModel):
    raw_text: str
    matched_product_name: str
    match_confidence: float = Field(..., description="Fuzzy match similarity score, 0-1.")
    brand: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    quantity: float
    current_price: float = Field(..., description="Today's lowest price across vendors, per unit.")
    estimated_line_total: float
    status: str
    recommendation: str
    percentage_difference_from_average: float


class UnmatchedPOItem(BaseModel):
    raw_text: str
    quantity: float
    reason: str
    suggestions: List[str] = Field(default_factory=list)


class POAnalysisResponse(BaseModel):
    matched_items: List[MatchedPOItem]
    unmatched_items: List[UnmatchedPOItem]
    matched_count: int
    unmatched_count: int
    total_estimated_cost: float


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------
def _best_match(raw_text: str, catalog_names: List[str]) -> Optional[tuple[str, float]]:
    """
    Find the best-matching catalog product name for a raw PO line item.

    Strategy:
      1. If any catalog name is a case-insensitive substring of the raw
         text (or vice-versa), prefer that — handles cases like
         "100 bags UltraTech Cement 50kg for site A" containing the
         exact product name as a substring.
      2. Otherwise fall back to difflib fuzzy string similarity, which
         handles typos and reordered words.

    Returns (matched_name, confidence_score) or None if nothing clears
    MATCH_ACCEPT_THRESHOLD.
    """
    normalized_text = raw_text.strip().lower()
    if not normalized_text:
        return None

    # --- Pass 1: substring containment (high-confidence, cheap check) ---
    substring_candidates = [
        name for name in catalog_names
        if name.lower() in normalized_text or normalized_text in name.lower()
    ]
    if substring_candidates:
        # Prefer the longest matching name — reduces false positives from
        # short/generic catalog names matching too eagerly.
        best = max(substring_candidates, key=len)
        return best, 0.95

    # --- Pass 2: fuzzy similarity fallback ---
    close_matches = difflib.get_close_matches(
        normalized_text, [n.lower() for n in catalog_names], n=1, cutoff=MATCH_ACCEPT_THRESHOLD
    )
    if close_matches:
        matched_lower = close_matches[0]
        # Map back to the original-cased catalog name.
        original_name = next(n for n in catalog_names if n.lower() == matched_lower)
        score = difflib.SequenceMatcher(None, normalized_text, matched_lower).ratio()
        return original_name, round(score, 2)

    return None


def _suggest_candidates(raw_text: str, catalog_names: List[str]) -> List[str]:
    """Generate low-confidence 'did you mean?' suggestions for an unmatched line item."""
    normalized_text = raw_text.strip().lower()
    close_matches = difflib.get_close_matches(
        normalized_text, [n.lower() for n in catalog_names],
        n=MAX_SUGGESTIONS, cutoff=SUGGESTION_THRESHOLD,
    )
    # Map back to original casing, preserving order, de-duplicated.
    seen = set()
    suggestions = []
    for match in close_matches:
        original = next((n for n in catalog_names if n.lower() == match), None)
        if original and original not in seen:
            suggestions.append(original)
            seen.add(original)
    return suggestions


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------
async def analyze_po(session: AsyncSession, items: List[POLineItem]) -> POAnalysisResponse:
    """
    Match every PO line item against the product catalog and compute
    price intelligence + estimated cost for each matched item.
    """
    catalog_result = await session.execute(select(Product.name))
    catalog_names = [row[0] for row in catalog_result.all()]

    matched_items: List[MatchedPOItem] = []
    unmatched_items: List[UnmatchedPOItem] = []
    total_cost = 0.0

    if not catalog_names:
        # No products in the catalog at all — every line item is unmatched.
        for item in items:
            unmatched_items.append(
                UnmatchedPOItem(
                    raw_text=item.raw_text,
                    quantity=item.quantity,
                    reason="Product catalog is empty.",
                    suggestions=[],
                )
            )
        return POAnalysisResponse(
            matched_items=[],
            unmatched_items=unmatched_items,
            matched_count=0,
            unmatched_count=len(unmatched_items),
            total_estimated_cost=0.0,
        )

    for item in items:
        try:
            match = _best_match(item.raw_text, catalog_names)

            if match is None:
                unmatched_items.append(
                    UnmatchedPOItem(
                        raw_text=item.raw_text,
                        quantity=item.quantity,
                        reason="No sufficiently similar product found in catalog.",
                        suggestions=_suggest_candidates(item.raw_text, catalog_names),
                    )
                )
                continue

            matched_name, confidence = match

            # Reuse the existing analytics engine — no duplicated business logic.
            summary = await compute_market_summary(session, matched_name)

            line_total = round(summary.todays_lowest_price * item.quantity, 2)
            total_cost += line_total

            matched_items.append(
                MatchedPOItem(
                    raw_text=item.raw_text,
                    matched_product_name=summary.product_name,
                    match_confidence=confidence,
                    brand=summary.brand,
                    category=summary.category,
                    unit=summary.unit,
                    quantity=item.quantity,
                    current_price=summary.todays_lowest_price,
                    estimated_line_total=line_total,
                    status=summary.status,
                    recommendation=summary.recommendation,
                    percentage_difference_from_average=summary.percentage_difference_from_average,
                )
            )
        except LookupError as exc:
            # Matched a product name but it somehow has no price history —
            # treat as unmatched rather than failing the whole PO.
            logger.warning("Matched product but analytics lookup failed for %r: %s", item.raw_text, exc)
            unmatched_items.append(
                UnmatchedPOItem(
                    raw_text=item.raw_text,
                    quantity=item.quantity,
                    reason=str(exc),
                    suggestions=[],
                )
            )
        except Exception:
            logger.exception("Unexpected error processing PO line item %r.", item.raw_text)
            unmatched_items.append(
                UnmatchedPOItem(
                    raw_text=item.raw_text,
                    quantity=item.quantity,
                    reason="Internal error while analyzing this line item.",
                    suggestions=[],
                )
            )

    return POAnalysisResponse(
        matched_items=matched_items,
        unmatched_items=unmatched_items,
        matched_count=len(matched_items),
        unmatched_count=len(unmatched_items),
        total_estimated_cost=round(total_cost, 2),
    )