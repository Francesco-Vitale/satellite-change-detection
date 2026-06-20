"""
Public API routes.

Per the brief, the frontend only needs:
    GET /api/changes        - list (sidebar)
    GET /api/changes/{id}   - detail (map polygons)

Two extras are included because they make the project demonstrably real
rather than a static mock: POST /api/changes/detect actually runs the
STAC + rasterio pipeline (live mode only - see DEMO_MODE.md), and
GET /api/changes/geojson gives the map a single fetch for all polygons
at once instead of N detail calls.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.models.change_event import ChangeEvent
from app.schemas.change_event import (
    ChangeEventDetail,
    ChangeEventList,
    ChangeEventSummary,
    PipelineRunRequest,
    PipelineRunResponse,
)

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api", tags=["changes"])


@router.get("/changes", response_model=ChangeEventList)
async def list_changes(db: AsyncSession = Depends(get_db)) -> ChangeEventList:
    result = await db.execute(select(ChangeEvent).order_by(ChangeEvent.severity_score.desc()))
    events = result.scalars().all()
    return ChangeEventList(
        items=[ChangeEventSummary.model_validate(e) for e in events],
        total=len(events),
    )


@router.get("/changes/geojson")
async def changes_geojson(db: AsyncSession = Depends(get_db)) -> dict:
    """All change polygons in one FeatureCollection, each feature tagged
    with its parent event_id/severity so the map can style + link them."""
    result = await db.execute(select(ChangeEvent))
    events = result.scalars().all()

    features = []
    for event in events:
        for feature in event.bbox_geojson.get("features", []):
            feature = dict(feature)
            props = dict(feature.get("properties", {}))
            props["event_id"] = event.id
            props["tile_id"] = event.tile_id
            feature["properties"] = props
            features.append(feature)

    return {"type": "FeatureCollection", "features": features}


@router.get("/changes/{event_id}", response_model=ChangeEventDetail)
async def get_change(event_id: int, db: AsyncSession = Depends(get_db)) -> ChangeEventDetail:
    event = await db.get(ChangeEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Change event {event_id} not found")
    return ChangeEventDetail.model_validate(event)


@router.post("/changes/detect", response_model=PipelineRunResponse)
async def run_detection(
    body: PipelineRunRequest, db: AsyncSession = Depends(get_db)
) -> PipelineRunResponse:
    """
    Run the real STAC -> rasterio -> NDVI-delta pipeline against the live
    Planetary Computer catalog and persist the result.

    Disabled while DEMO_MODE=true (the default in this build, since the
    environment it was built in has no internet access - see
    DEMO_MODE.md). Set DEMO_MODE=false once you have network access to
    planetarycomputer.microsoft.com to use this for real.
    """
    if settings.demo_mode:
        raise HTTPException(
            status_code=503,
            detail=(
                "Live detection is disabled while DEMO_MODE=true. "
                "Set DEMO_MODE=false in your environment to query the real "
                "STAC catalog (requires internet access). See DEMO_MODE.md."
            ),
        )

    # Imported lazily so demo-mode deployments don't need rasterio's heavy
    # GDAL dependency chain to import successfully at startup.
    from app.services.ndvi_change import compute_change
    from app.services.stac_client import StacScenePicker

    bbox = body.bbox or settings.default_bbox
    tile_id = body.tile_id or settings.default_mgrs_tile

    picker = StacScenePicker()
    try:
        before_item, after_item = picker.find_scene_pair(bbox, months_apart=body.months_apart)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    before_scene = picker.to_scene_ref(before_item)
    after_scene = picker.to_scene_ref(after_item)

    result = compute_change(before_scene, after_scene, bbox)

    event = ChangeEvent(
        tile_id=tile_id,
        date_before=before_scene.datetime_utc.date(),
        date_after=after_scene.datetime_utc.date(),
        bbox_geojson=result.geojson,
        change_area_km2=result.change_area_km2,
        severity_score=result.severity_score,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    return PipelineRunResponse(
        event=ChangeEventDetail.model_validate(event),
        scenes_used={"before": before_scene.item_id, "after": after_scene.item_id},
    )
