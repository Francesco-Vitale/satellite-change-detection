"""Pydantic (de)serialization schemas for the public API."""
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChangeEventSummary(BaseModel):
    """Lightweight shape returned by GET /api/changes (list view).

    Excludes the full GeoJSON geometry to keep the list endpoint cheap -
    the sidebar only needs id/tile/dates/area/severity. The map layer is
    fetched separately (see ChangeEventDetail) or via /api/changes/geojson.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    tile_id: str
    date_before: date
    date_after: date
    change_area_km2: float = Field(..., description="Total changed area in km^2")
    severity_score: float = Field(..., ge=0, le=1, description="Mean |delta NDVI| in changed pixels, 0-1")
    created_at: datetime


class ChangeEventDetail(ChangeEventSummary):
    """Full shape returned by GET /api/changes/{id}, including geometry."""

    bbox_geojson: dict[str, Any] = Field(
        ..., description="GeoJSON FeatureCollection of changed-pixel polygons"
    )


class ChangeEventList(BaseModel):
    items: list[ChangeEventSummary]
    total: int


class PipelineRunRequest(BaseModel):
    """Body for POST /api/changes/detect - trigger a fresh detection run."""

    tile_id: str | None = Field(None, description="MGRS tile id, e.g. '33UXP'")
    bbox: list[float] | None = Field(
        None, min_length=4, max_length=4, description="[min_lon, min_lat, max_lon, max_lat]"
    )
    months_apart: int = Field(6, ge=1, le=24, description="Target gap between the two scenes")


class PipelineRunResponse(BaseModel):
    event: ChangeEventDetail
    scenes_used: dict[str, str] = Field(
        ..., description="STAC item ids used for the before/after scenes"
    )
