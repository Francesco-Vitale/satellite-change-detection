"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.routes import router
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal, Base, engine
from app.models.change_event import ChangeEvent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


async def _create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_demo_data_if_empty() -> None:
    """In demo mode, populate change_events with synthetic-but-realistic
    data on first boot so the frontend has something to render without
    requiring internet access. No-op if rows already exist, and a no-op
    entirely once DEMO_MODE=false."""
    if not settings.demo_mode:
        return

    async with AsyncSessionLocal() as session:
        existing = await session.execute(select(ChangeEvent.id).limit(1))
        if existing.first() is not None:
            logger.info("change_events already populated, skipping demo seed")
            return

        from app.services.demo_data import all_synthetic_events

        logger.info("DEMO_MODE=true and table empty: seeding synthetic demo events")
        for data in all_synthetic_events():
            session.add(ChangeEvent(**data))
        await session.commit()
        logger.info("Seeded %d synthetic demo change events", len(all_synthetic_events()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _create_tables()
    await _seed_demo_data_if_empty()
    yield


app = FastAPI(
    title="Satellite Change Detection API",
    description=(
        "Detects land-cover change between two Sentinel-2 scenes using "
        "NDVI delta thresholding. See /docs for the interactive API explorer."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "demo_mode": settings.demo_mode}
