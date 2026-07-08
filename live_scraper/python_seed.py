# seed.py
import asyncio
from live_scraper.database import init_db, get_session
from demo_data.models import Product

async def seed():
    await init_db()
    async with get_session() as session:
        session.add_all([
            Product(name="UltraTech Cement 50kg", category="Cement"),
            Product(name="ACC Gold Cement 50kg", category="Cement"),
            Product(name="TMT Steel Bar 12mm", category="Steel"),
        ])

if __name__ == "__main__":
    asyncio.run(seed())