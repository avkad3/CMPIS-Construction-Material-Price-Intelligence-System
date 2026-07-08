"""
app.py
------
Streamlit frontend for CMPIS (Construction Material Price Intelligence System).

Architecture note: this app is a PURE presentation layer. It never touches
the database directly — it only calls the FastAPI backend's REST endpoints
over HTTP (requests library), exactly the way a React frontend would. This
keeps the same clean separation of concerns: swap this file for a React app
later without touching backend/ at all, or point it at a different backend
deployment just by changing API_BASE_URL.

Two pages:
  1. Product Dashboard — browse/search a single product's price intelligence.
  2. Purchase Order Analyzer — paste or upload a PO and see matched product
     details, current pricing, and an estimated total cost.

Run with:
    streamlit run app.py

Requires the FastAPI backend (main.py) to be running separately, e.g.:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import io
import os
from typing import Any, Dict, List, Optional

import requests
import streamlit as st
import plotly.graph_objects as go
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE_URL = os.getenv("CMPIS_API_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT_SECONDS = 10

STATUS_COLORS = {
    "GREEN": "#1DB954",
    "YELLOW": "#F2C744",
    "RED": "#E63946",
}
STATUS_LABELS = {
    "GREEN": "🟢 GREEN — Buy Now",
    "YELLOW": "🟡 YELLOW — Fair Market Price",
    "RED": "🔴 RED — Wait if Possible",
}

st.set_page_config(
    page_title="CMPIS — Price Intelligence",
    page_icon="🏗️",
    layout="wide",
)


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------
def _api_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """
    Thin wrapper around requests.get with consistent error handling.
    Returns None (and shows a Streamlit error) instead of raising, so a
    single failed call never crashes the whole page.
    """
    url = f"{API_BASE_URL}{path}"
    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code == 404:
            st.warning(response.json().get("detail", "Not found."))
            return None
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError:
        st.error(
            f"Could not connect to the CMPIS backend at `{API_BASE_URL}`. "
            "Make sure it's running (e.g. `uvicorn main:app --reload --port 8000`)."
        )
        return None
    except requests.exceptions.Timeout:
        st.error("The backend took too long to respond. Please try again.")
        return None
    except requests.exceptions.RequestException as exc:
        st.error(f"Unexpected error calling the backend: {exc}")
        return None


def _api_post(path: str, json_body: dict) -> Optional[dict]:
    """POST wrapper mirroring _api_get's error handling for the PO analysis call."""
    url = f"{API_BASE_URL}{path}"
    try:
        response = requests.post(url, json=json_body, timeout=REQUEST_TIMEOUT_SECONDS * 3)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError:
        st.error(
            f"Could not connect to the CMPIS backend at `{API_BASE_URL}`. "
            "Make sure it's running (e.g. `uvicorn main:app --reload --port 8000`)."
        )
        return None
    except requests.exceptions.Timeout:
        st.error("The backend took too long to respond while analyzing the PO. Please try again.")
        return None
    except requests.exceptions.RequestException as exc:
        st.error(f"Unexpected error calling the backend: {exc}")
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_all_products() -> List[Dict[str, Any]]:
    """Cached product catalog listing — refreshes at most every 5 minutes."""
    data = _api_get("/products")
    return data["products"] if data else []


def fetch_summary(product_name: str) -> Optional[dict]:
    return _api_get(f"/summary/{product_name}")


def fetch_history(product_name: str) -> Optional[dict]:
    return _api_get(f"/history/{product_name}")


def fetch_vendors(product_name: str) -> Optional[dict]:
    return _api_get(f"/vendors/{product_name}")


def search_products(query: str) -> List[Dict[str, Any]]:
    data = _api_get(f"/search/{query}")
    return data["results"] if data else []


def analyze_po(items: List[Dict[str, Any]]) -> Optional[dict]:
    """Send parsed PO line items to the backend for matching + price analysis."""
    return _api_post("/po/analyze", {"items": items})


def fetch_data_status() -> Optional[dict]:
    return _api_get("/admin/data-status")


def trigger_seed_demo(force: bool = False) -> Optional[dict]:
    return _api_post("/admin/seed-demo", {"force": force})


def trigger_scrape_live() -> Optional[dict]:
    return _api_post("/admin/scrape-live", {})


def trigger_reset() -> Optional[dict]:
    return _api_post("/admin/reset", {})


# ---------------------------------------------------------------------------
# PO parsing helpers (frontend concern — backend only accepts clean
# {raw_text, quantity} pairs, so all file/text parsing happens here)
# ---------------------------------------------------------------------------
def parse_pasted_text(raw_text: str) -> List[Dict[str, Any]]:
    """
    Parse freeform pasted PO text, one line item per line.
    Accepted formats per line:
        Product description, quantity
        Product description x quantity
        Product description          (quantity defaults to 1)
    """
    items: List[Dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        quantity = 1.0
        description = line

        if "," in line:
            parts = line.rsplit(",", 1)
            possible_qty = parts[1].strip()
            if possible_qty.replace(".", "", 1).isdigit():
                description = parts[0].strip()
                quantity = float(possible_qty)
        elif " x " in line.lower():
            # Handle "Product Name x 100" (case-insensitive separator)
            lower_line = line.lower()
            split_idx = lower_line.rfind(" x ")
            possible_qty = line[split_idx + 3:].strip()
            if possible_qty.replace(".", "", 1).isdigit():
                description = line[:split_idx].strip()
                quantity = float(possible_qty)

        items.append({"raw_text": description, "quantity": quantity})

    return items


def parse_uploaded_file(uploaded_file) -> List[Dict[str, Any]]:
    """
    Parse an uploaded CSV/Excel PO file. Expects a product-description
    column (any of: 'product', 'product name', 'description', 'item')
    and optionally a quantity column (any of: 'quantity', 'qty').
    Column matching is case-insensitive and whitespace-tolerant.
    """
    filename = uploaded_file.name.lower()
    if filename.endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    elif filename.endswith((".xlsx", ".xls")):
        df = pd.read_excel(uploaded_file)
    else:
        st.error("Unsupported file type. Please upload a .csv or .xlsx file.")
        return []

    normalized_cols = {c.strip().lower(): c for c in df.columns}

    description_col = next(
        (normalized_cols[c] for c in ("product", "product name", "description", "item", "item description") if c in normalized_cols),
        None,
    )
    quantity_col = next(
        (normalized_cols[c] for c in ("quantity", "qty") if c in normalized_cols),
        None,
    )

    if description_col is None:
        st.error(
            "Could not find a product/description column in the uploaded file. "
            "Expected a column named one of: Product, Product Name, Description, Item."
        )
        return []

    items: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        description = str(row[description_col]).strip()
        if not description or description.lower() == "nan":
            continue
        quantity = 1.0
        if quantity_col is not None:
            try:
                quantity = float(row[quantity_col])
                if quantity <= 0:
                    quantity = 1.0
            except (ValueError, TypeError):
                quantity = 1.0
        items.append({"raw_text": description, "quantity": quantity})

    return items


# ---------------------------------------------------------------------------
# Sidebar — page navigation (shared across both pages)
# ---------------------------------------------------------------------------
st.sidebar.title("🏗️ CMPIS")
st.sidebar.caption("Construction Material Price Intelligence System")

page = st.sidebar.radio(
    "View",
    ["📊 Product Dashboard", "📄 Purchase Order Analyzer", "⚙️ Data Management"],
)

st.sidebar.divider()


# ---------------------------------------------------------------------------
# PAGE 1: Product Dashboard
# ---------------------------------------------------------------------------
if page == "📊 Product Dashboard":

    products = fetch_all_products()

    if not products:
        st.sidebar.warning("No products available. Is the backend running and seeded?")
        st.stop()

    categories = sorted({p["category"] for p in products if p.get("category")})
    selected_category = st.sidebar.selectbox("Category", ["All"] + categories)

    filtered_products = (
        products if selected_category == "All"
        else [p for p in products if p["category"] == selected_category]
    )
    product_names = sorted(p["name"] for p in filtered_products)

    selected_product = st.sidebar.selectbox("Product", product_names)

    st.sidebar.divider()
    search_query = st.sidebar.text_input("🔍 Quick search", placeholder="e.g. cement, TMT bar...")
    if search_query:
        search_results = search_products(search_query)
        if search_results:
            st.sidebar.caption(f"{len(search_results)} match(es):")
            for r in search_results:
                if st.sidebar.button(r["name"], key=f"search_{r['id']}"):
                    selected_product = r["name"]
        else:
            st.sidebar.caption("No matches found.")

    if st.sidebar.button("🔄 Refresh data"):
        fetch_all_products.clear()
        st.rerun()

    if not selected_product:
        st.info("Select a product from the sidebar to view its market intelligence.")
        st.stop()

    st.title(selected_product)

    summary = fetch_summary(selected_product)

    if summary is None:
        st.stop()  # error/warning already shown by _api_get

    # --- Current Price headline ---------------------------------------------
    # "Current price" = today's lowest price across all vendors — the actual
    # price a buyer could pay right now. Deliberately the most prominent
    # number on the page.
    current_price = summary["todays_lowest_price"]
    unit_label = f" / {summary['unit']}" if summary.get("unit") else ""

    st.markdown(
        f"""
        <div style="margin-bottom: 6px;">
            <span style="font-size: 15px; color: #666;">Current Price (best available today)</span><br>
            <span style="font-size: 42px; font-weight: 700; color: #1a1a1a;">₹{current_price:,.2f}</span>
            <span style="font-size: 16px; color: #888;">{unit_label}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # --- Status banner --------------------------------------------------------
    status = summary["status"]
    status_color = STATUS_COLORS.get(status, "#888888")
    status_label = STATUS_LABELS.get(status, status)

    st.markdown(
        f"""
        <div style="padding: 14px 20px; border-radius: 10px; background-color: {status_color}22;
                    border-left: 6px solid {status_color}; margin-bottom: 18px;">
            <span style="font-size: 20px; font-weight: 600; color: {status_color};">
                {status_label}
            </span>
            <span style="font-size: 15px; color: #444; margin-left: 12px;">
                {summary['recommendation']} — {summary['percentage_difference_from_average']:+.2f}% vs. historical average
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        f"Brand: **{summary.get('brand') or '—'}** · "
        f"Category: **{summary.get('category') or '—'}** · "
        f"Unit: **{summary.get('unit') or '—'}** · "
        f"Data as of **{summary['today_date']}** · "
        f"{summary['data_points_considered']} historical data points"
    )

    # --- Key metrics row 1: today's prices ------------------------------------
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Today's Lowest Price", f"₹{summary['todays_lowest_price']:,.2f}")
    col2.metric("Today's Highest Price", f"₹{summary['todays_highest_price']:,.2f}")
    col3.metric("Today's Average Price", f"₹{summary['todays_average_price']:,.2f}")
    col4.metric(
        "30-Day Change",
        f"{summary['price_change_30d_pct']:+.2f}%" if summary["price_change_30d_pct"] is not None else "N/A",
    )

    # --- Key metrics row 2: historical stats -----------------------------------
    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Historical Average", f"₹{summary['historical_average_price']:,.2f}")
    col6.metric("Historical Min", f"₹{summary['historical_min_price']:,.2f}")
    col7.metric("Historical Max", f"₹{summary['historical_max_price']:,.2f}")
    col8.metric("Volatility", f"{summary['price_volatility_pct']:.2f}%")

    # --- Key metrics row 3: vendor + trend -------------------------------------
    col9, col10, col11 = st.columns(3)
    col9.metric("Cheapest Vendor Today", summary["cheapest_vendor_today"])
    col10.metric("Most Expensive Vendor Today", summary["most_expensive_vendor_today"])

    trend_icons = {"RISING": "📈 Rising", "FALLING": "📉 Falling", "STABLE": "➡️ Stable", "INSUFFICIENT_DATA": "— N/A"}
    col11.metric("Market Trend", trend_icons.get(summary["market_trend"], summary["market_trend"]))

    st.divider()

    # --- Price history chart ---------------------------------------------------
    st.subheader("📊 180-Day Price History")

    history = fetch_history(selected_product)
    if history and history["history"]:
        df_history = pd.DataFrame(history["history"])
        df_history["date"] = pd.to_datetime(df_history["date"])

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=df_history["date"],
                y=df_history["price"],
                mode="lines",
                name="Daily Average Price",
                line=dict(color="#2E86AB", width=2),
                fill="tozeroy",
                fillcolor="rgba(46, 134, 171, 0.1)",
            )
        )
        fig.add_hline(
            y=summary["historical_average_price"],
            line_dash="dash",
            line_color="gray",
            annotation_text="Historical Average",
            annotation_position="top left",
        )
        unit_suffix = f" / {summary['unit']}" if summary.get("unit") else ""
        fig.update_layout(
            height=400,
            margin=dict(l=10, r=10, t=30, b=10),
            yaxis_title=f"Price (INR{unit_suffix})",
            xaxis_title=None,
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No historical chart data available for this product.")

    st.divider()

    # --- Vendor comparison -------------------------------------------------------
    st.subheader("🏬 Vendor Comparison")

    vendors_data = fetch_vendors(selected_product)
    if vendors_data and vendors_data["vendors"]:
        df_vendors = pd.DataFrame(vendors_data["vendors"])
        df_vendors_display = df_vendors.rename(
            columns={
                "vendor": "Vendor",
                "todays_price": "Today's Price (₹)",
                "historical_average_price": "Historical Avg (₹)",
                "purchase_link": "Link",
            }
        )

        left, right = st.columns([3, 2])

        with left:
            st.dataframe(
                df_vendors_display[["Vendor", "Today's Price (₹)", "Historical Avg (₹)"]],
                use_container_width=True,
                hide_index=True,
            )
            for row in vendors_data["vendors"]:
                if row.get("purchase_link"):
                    st.caption(f"🔗 [{row['vendor']}]({row['purchase_link']}) — ₹{row['todays_price']:,.2f}")

        with right:
            bar_fig = go.Figure()
            bar_fig.add_trace(
                go.Bar(
                    x=df_vendors["vendor"],
                    y=df_vendors["todays_price"],
                    marker_color="#2E86AB",
                    name="Today's Price",
                )
            )
            bar_fig.update_layout(
                height=320,
                margin=dict(l=10, r=10, t=30, b=10),
                yaxis_title="Price (₹)",
                xaxis_title=None,
            )
            st.plotly_chart(bar_fig, use_container_width=True)
    else:
        st.info("No vendor comparison data available for this product.")


# ---------------------------------------------------------------------------
# PAGE 2: Purchase Order Analyzer
# ---------------------------------------------------------------------------
elif page == "📄 Purchase Order Analyzer":
    st.title("📄 Purchase Order Analyzer")
    st.caption(
        "Paste your PO line items or upload a CSV/Excel file. Each product will be "
        "matched against the catalog and priced using today's market data."
    )

    input_mode = st.radio("Input method", ["Paste text", "Upload file"], horizontal=True)

    parsed_items: List[Dict[str, Any]] = []

    if input_mode == "Paste text":
        st.caption(
            "One line item per line. Supported formats: "
            "`Product name, quantity` · `Product name x quantity` · `Product name` (quantity defaults to 1)"
        )
        pasted_text = st.text_area(
            "PO line items",
            height=200,
            placeholder="UltraTech Cement 50kg, 100\nTMT Bar 12mm x 500\nRiver Sand",
        )
        if pasted_text.strip():
            parsed_items = parse_pasted_text(pasted_text)

    else:
        st.caption(
            "Upload a .csv or .xlsx file with a product/description column "
            "(e.g. 'Product Name') and optionally a quantity column (e.g. 'Qty')."
        )
        uploaded_file = st.file_uploader("PO file", type=["csv", "xlsx", "xls"])
        if uploaded_file is not None:
            parsed_items = parse_uploaded_file(uploaded_file)

    if parsed_items:
        st.write(f"**{len(parsed_items)} line item(s) parsed:**")
        st.dataframe(pd.DataFrame(parsed_items), use_container_width=True, hide_index=True)

        if st.button("🔍 Analyze PO", type="primary"):
            with st.spinner("Matching products and fetching live pricing..."):
                result = analyze_po(parsed_items)

            if result is None:
                st.stop()

            st.divider()

            # --- Summary metrics ---------------------------------------------
            col1, col2, col3 = st.columns(3)
            col1.metric("Matched Items", result["matched_count"])
            col2.metric("Unmatched Items", result["unmatched_count"])
            col3.metric("Estimated Total Cost", f"₹{result['total_estimated_cost']:,.2f}")

            # --- Matched items table ------------------------------------------
            if result["matched_items"]:
                st.subheader("✅ Matched Products")
                df_matched = pd.DataFrame(result["matched_items"])

                def _status_badge(row) -> str:
                    color = STATUS_COLORS.get(row["status"], "#888888")
                    return f'<span style="color:{color}; font-weight:600;">{row["status"]}</span>'

                display_df = df_matched.copy()
                display_df["Status"] = display_df.apply(_status_badge, axis=1)
                display_df = display_df.rename(
                    columns={
                        "raw_text": "PO Line Item",
                        "matched_product_name": "Matched Product",
                        "quantity": "Qty",
                        "unit": "Unit",
                        "current_price": "Current Price (₹)",
                        "estimated_line_total": "Line Total (₹)",
                        "recommendation": "Recommendation",
                    }
                )
                cols_to_show = [
                    "PO Line Item", "Matched Product", "Qty", "Unit",
                    "Current Price (₹)", "Line Total (₹)", "Status", "Recommendation",
                ]
                st.write(
                    display_df[cols_to_show].to_html(escape=False, index=False),
                    unsafe_allow_html=True,
                )
            else:
                st.info("No line items were successfully matched to the catalog.")

            # --- Unmatched items -----------------------------------------------
            if result["unmatched_items"]:
                st.subheader("⚠️ Unmatched Items")
                for item in result["unmatched_items"]:
                    with st.expander(f"❌ {item['raw_text']} (qty: {item['quantity']:g})"):
                        st.write(f"**Reason:** {item['reason']}")
                        if item["suggestions"]:
                            st.write("**Did you mean:**")
                            for suggestion in item["suggestions"]:
                                st.write(f"- {suggestion}")
                        else:
                            st.write("No close matches found in the catalog.")
    else:
        st.info("Paste PO text or upload a file above to get started.")


# ---------------------------------------------------------------------------
# PAGE 3: Data Management (admin)
# ---------------------------------------------------------------------------
else:
    st.title("⚙️ Data Management")
    st.caption("Inspect what data is loaded, seed demo data, trigger a live scrape, or reset everything.")

    status = fetch_data_status()

    if status:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Products", status["product_count"])
        col2.metric("Demo Rows", status["demo_rows"])
        col3.metric("Live Rows", status["live_rows"])
        col4.metric("Total Price Rows", status["price_history_total"])

        if status["latest_collected_at"]:
            st.caption(f"Most recent data collected at: **{status['latest_collected_at']}**")
        else:
            st.caption("No data collected yet.")

    st.divider()

    col_a, col_b, col_c = st.columns(3)

    with col_a:
        st.subheader("🌱 Seed Demo Data")
        st.caption("Generates 180 days of simulated market data. No-ops if data already exists unless forced.")
        force_reseed = st.checkbox("Force re-seed (wipes existing data first)", value=False)
        if st.button("Run Demo Seed"):
            with st.spinner("Seeding demo data..."):
                result = trigger_seed_demo(force=force_reseed)
            if result:
                st.success(
                    f"Seeded {result['products_seeded']} product(s), "
                    f"{result['price_rows_created']} price row(s)."
                )
                fetch_all_products.clear()

    with col_b:
        st.subheader("🔴 Live Scrape")
        st.caption("Fetches real current prices from Serper.dev for every product in the catalog.")
        if st.button("Run Live Scrape"):
            with st.spinner("Scraping live prices — this may take a while..."):
                result = trigger_scrape_live()
            if result:
                st.success(f"Live scrape complete: {result}")
                fetch_all_products.clear()

    with col_c:
        st.subheader("🗑️ Reset All Data")
        st.caption("Permanently deletes every product and price record. This cannot be undone.")
        confirm_reset = st.checkbox("I understand this is permanent", value=False)
        if st.button("Reset Everything", type="secondary", disabled=not confirm_reset):
            with st.spinner("Deleting all data..."):
                result = trigger_reset()
            if result:
                st.success("All data has been reset.")
                fetch_all_products.clear()
                st.rerun()