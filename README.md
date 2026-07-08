#  CMPIS – Construction Material Price Intelligence System

A procurement intelligence platform that helps users compare construction material prices using historical market data, vendor comparisons, and Purchase Order (PO) analysis. The system is designed to assist procurement teams in evaluating whether current prices are fair based on historical market trends.

---

#  Overview

Construction material prices vary significantly across suppliers and over time. Procurement teams often spend considerable effort comparing quotations manually, increasing the risk of overpaying and delaying purchasing decisions.

The **Construction Material Price Intelligence System (CMPIS)** centralizes construction material pricing information into a single platform. Users can search products, compare vendor prices, analyze six months of historical pricing, and upload Purchase Orders (POs) for automated product matching and market analysis.

For demonstration purposes, the application uses **synthetically generated historical price data**. During the first startup, the database is automatically populated using a **data seeding process**, generating approximately six months of realistic pricing history for multiple products and vendors. This allows the analytics engine and dashboard to function without requiring live web scraping.

The system follows a modular architecture where data collection, analytics, and presentation are completely separated, making it easy to replace the seeded data with real market data in future deployments.

---

#  Features

## Current Features

- 🔍 Product catalog browsing and search
- 📊 Six-month historical price analysis
- 🏪 Multi-vendor price comparison
- 📈 Historical price trend visualization
- 🚦 Market status indicator (Green / Yellow / Red)
- 📄 Purchase Order (PO) upload and analysis
- 🔎 Product matching using fuzzy search
- ⚡ FastAPI REST API
- 🎨 Streamlit dashboard
- 💾 SQLite database
- 🌱 Automatic demo data seeding
- 🧩 Modular architecture for future live data integration

---

#  System Architecture

```text
                    User
                      │
                      ▼
             Streamlit Frontend
                      │
               REST API Requests
                      │
                      ▼
               FastAPI Backend
                      │
         ┌────────────┴────────────┐
         │                         │
         ▼                         ▼
 Analytics Engine          Product Search
         │
         ▼
   SQLite Database
         │
         ▼
 Historical Price Data
   (Seeded Demo Dataset)

          Optional

   Live Scraper Worker
         │
         ▼
   Serper.dev Shopping API
```

---

#  Project Structure

```text
construction-price-intelligence/
│
├── app.py                     # Streamlit frontend
├── main.py                    # FastAPI backend
├── requirements.txt
├── README.md
│
├── demo_data/
│   ├── database.py
│   ├── models.py
│   ├── analytics.py
│   ├── service.py
│   ├── seed_demo_data.py
│   └── ...
│
├── live_scraper/
│   ├── database.py
│   ├── worker.py
│   ├── service.py
│   └── ...
│
└── cmpis.db
```

---

#  Market Intelligence

For every construction material, CMPIS provides:

- Current market price
- Lowest available vendor price
- Historical average price
- Six-month price history
- Vendor comparison
- Price deviation from historical average
- Market status recommendation

Example:

| Metric | Value |
|---------|--------|
| Product | UltraTech Cement 50kg |
| Lowest Price | ₹385 |
| Historical Average | ₹398 |
| Difference | -3.2% |
| Status | 🟢 Green |

---

#  Market Status Logic

The application compares today's price with historical pricing trends.

### 🟢 Green

Current price is significantly below the historical average.

**Recommendation**

Good opportunity to purchase.

---

### 🟡 Yellow

Current price is close to the historical average.

**Recommendation**

Fair market pricing.

---

### 🔴 Red

Current price is significantly above the historical average.

**Recommendation**

Consider comparing additional suppliers before purchasing.

---

#  Purchase Order Analysis

Users can upload a Purchase Order (PO) containing construction materials.

The system automatically:

- Extracts product names
- Matches products against the product catalog
- Retrieves historical pricing
- Compares vendor prices
- Displays market status for each matched item

---

#  Database Schema

## Products

| Column | Type |
|---------|------|
| id | Integer |
| name | String |
| category | String |

---

## Price History

| Column | Type |
|---------|------|
| id | Integer |
| product_id | Foreign Key |
| source | String |
| price | Float |
| date | Date |
| purchase_link | String |

---

#  Technology Stack

## Backend

- Python
- FastAPI
- SQLAlchemy 2.0
- AsyncIO
- SQLite
- Pydantic

## Frontend

- Streamlit

## Data Processing

- Pandas
- Requests
- HTTPX

## Optional Live Data Collection

- Serper.dev Shopping API

---

#  Installation

## Clone the Repository

```bash
git clone <repository-url>
cd construction-price-intelligence
```

---

## Create a Virtual Environment

Windows

```powershell
python -m venv .venv
```

Activate

```powershell
.\.venv\Scripts\Activate.ps1
```

---

## Install Dependencies

```powershell
pip install -r requirements.txt
```

If a requirements file is unavailable:

```powershell
pip install fastapi uvicorn sqlalchemy aiosqlite pydantic streamlit requests plotly pandas httpx rapidfuzz python-multipart
```

---

## Configure Environment Variables

Create a `.env` file or configure your environment:

```env
CMPIS_DATABASE_URL=sqlite:///./cmpis.db
SERPER_API_KEY=your_serper_api_key
```

> **Note:** `SERPER_API_KEY` is only required when using the optional live scraper.

---

#  Running the Backend

Start the FastAPI server:

```powershell
python -m uvicorn main:app --reload --port 8000
```

Available endpoints include:

| Method | Endpoint | Description |
|---------|----------|-------------|
| GET | `/products` | Retrieve all products |
| GET | `/search/{product_name}` | Search products |
| GET | `/summary/{product_name}` | Market summary |
| GET | `/history/{product_name}` | Historical prices |
| GET | `/vendors/{product_name}` | Vendor comparison |
| POST | `/po/analyze` | Analyze Purchase Order |
| GET | `/admin/data-status` | Check database status |
| POST | `/admin/seed-demo` | Seed demo data |
| POST | `/admin/scrape-live` | Trigger live scraping |
| POST | `/admin/reset` | Reset demo database |

---

#  Running the Frontend

Start the Streamlit application:

```powershell
python -m streamlit run app.py
```

Open the local URL displayed in the terminal.

---

#  Demo Data Seeding

The application is designed to work immediately after installation.

When the backend starts:

- Database tables are automatically created.
- If no products exist, the system seeds the database with synthetic data.
- Approximately **six months of realistic historical pricing** are generated for multiple construction materials and vendors.

The seeded dataset enables:

- Historical price charts
- Vendor comparison
- Market analytics
- Purchase Order analysis

without requiring live internet access.

---

#  Optional Live Price Collection

The project includes an optional live scraping module.

When configured with a valid **Serper.dev API key**, the worker can retrieve current market prices and store them in the database.

Run:

```powershell
python live_scraper/worker.py
```

The analytics layer is independent of the data source, allowing the application to seamlessly switch between seeded data and live market data.

---

#  Design Principles

The project follows a modular architecture:

- **Presentation Layer** – Streamlit UI
- **API Layer** – FastAPI
- **Business Logic Layer** – Analytics & Market Intelligence
- **Data Layer** – SQLite & SQLAlchemy
- **Data Collection Layer** – Seeded Demo Data / Live Scraper

This separation allows each component to evolve independently while keeping the application maintainable and scalable.

---

#  Future Enhancements

Planned improvements include:

- Multi-source live web scraping
- Automated background data collection
- Vendor reliability scoring
- Price anomaly detection
- AI-powered procurement recommendations
- Regional price comparison
- Price forecasting
- Email and WhatsApp alerts
- Interactive analytics dashboard
- Procurement risk scoring
- OCR-based Purchase Order extraction
- Machine learning-based price prediction

---

#  Notes

- SQLite is used as the default database.
- The database schema is created automatically on first startup.
- Historical pricing is generated through an automated data seeding process.
- The seeded dataset is intended for demonstration and development purposes.
- The frontend communicates with the backend exclusively through REST APIs.
- The optional live scraper can replace the seeded dataset without modifying the analytics engine.

---

#  Troubleshooting

### Backend not starting

- Verify that all dependencies are installed.
- Ensure the virtual environment is activated.

### Database errors

Delete `cmpis.db` and restart the backend to recreate the database.

### Live scraper issues

- Verify that `SERPER_API_KEY` is configured correctly.
- Ensure internet connectivity.

### Frontend cannot connect

Make sure the FastAPI backend is running before launching the Streamlit application.

---

#  Author

**Construction Material Price Intelligence System (CMPIS)**

A demonstration project showcasing procurement analytics, historical price intelligence, vendor comparison, Purchase Order analysis, and a scalable backend architecture using **Python**, **FastAPI**, **Streamlit**, **SQLAlchemy**, and **SQLite**.