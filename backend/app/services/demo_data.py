"""
Synthetic demo data generator.

This module exists ONLY because the environment this was built in had no
internet access to query the live STAC API (see DEMO_MODE.md at the repo
root for the full explanation). It generates change events using the
exact same NDVI-delta-threshold-vectorize math as the real pipeline
(`app.services.ndvi_change`), just fed with synthetic NDVI arrays instead
of real rasterio reads of Sentinel-2 COGs.

Nothing about the detection logic itself is faked - only the input
imagery is synthetic. Swapping `generate_synthetic_ndvi_pair` for a real
STAC fetch + rasterio read is the entire diff needed to go from demo mode
to live mode (see scripts/run_pipeline.py for that real path).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

import numpy as np
import rasterio
from rasterio.features import shapes as rasterio_shapes
from shapely.geometry import shape as shapely_shape

from app.core.config import get_settings

settings = get_settings()


@dataclass
class SyntheticEventSpec:
    tile_id: str
    date_before: date
    date_after: date
    bbox: list[float]  # WGS84 [min_lon, min_lat, max_lon, max_lat]
    seed: int
    label: str  # human-readable scenario name, used only for log/README context


# A handful of scenarios across the same real tile (33UXP, Vienna-Bratislava
# agricultural corridor) at different sub-areas and seasons, so the demo
# dataset reads as plausible seasonal agricultural change rather than one
# repeated number.
DEMO_SCENARIOS: list[SyntheticEventSpec] = [
    SyntheticEventSpec(
        tile_id="33UXP",
        date_before=date(2025, 4, 18),
        date_after=date(2025, 10, 12),
        bbox=[16.85, 48.05, 17.15, 48.25],
        seed=1,
        label="Marchfeld cropland, spring->autumn harvest cycle",
    ),
    SyntheticEventSpec(
        tile_id="33UXP",
        date_before=date(2025, 5, 2),
        date_after=date(2025, 11, 3),
        bbox=[16.55, 48.15, 16.85, 48.35],
        seed=2,
        label="Mixed farmland north of Vienna",
    ),
    SyntheticEventSpec(
        tile_id="33UXP",
        date_before=date(2025, 3, 29),
        date_after=date(2025, 9, 25),
        bbox=[17.05, 47.95, 17.35, 48.15],
        seed=3,
        label="Cropland near Bratislava border, drought-affected patch",
    ),
    SyntheticEventSpec(
        tile_id="33UXQ",
        date_before=date(2025, 4, 9),
        date_after=date(2025, 10, 1),
        bbox=[16.85, 48.25, 17.15, 48.45],
        seed=4,
        label="Forest/cropland mosaic, minor change",
    ),
]


def generate_synthetic_ndvi_pair(
    seed: int, shape: tuple[int, int] = (220, 220)
) -> tuple[np.ndarray, np.ndarray, rasterio.Affine]:
    """
    Produce a plausible pair of NDVI arrays for a "before" (growing
    season) and "after" (post-harvest / drought-stressed) scene.

    Base NDVI ~0.55-0.75 (healthy cropland) with a few patches dropped
    to ~0.1-0.3 in the "after" array to simulate harvested fields or
    vegetation loss, plus small uniform noise everywhere to avoid
    perfectly flat synthetic-looking regions.
    """
    rng = np.random.default_rng(seed)
    h, w = shape

    base = 0.55 + 0.15 * rng.random((h, w))
    noise = rng.normal(0, 0.03, (h, w))
    ndvi_before = np.clip(base + noise, -1, 1).astype("float32")

    ndvi_after = ndvi_before.copy()

    # Carve out 2-4 irregular "changed" patches (harvested fields / stress)
    n_patches = rng.integers(2, 5)
    for _ in range(n_patches):
        cy, cx = rng.integers(20, h - 20), rng.integers(20, w - 20)
        ry, rx = rng.integers(15, 45), rng.integers(15, 45)
        yy, xx = np.ogrid[:h, :w]
        patch = ((yy - cy) ** 2) / (ry ** 2) + ((xx - cx) ** 2) / (rx ** 2) <= 1
        drop = rng.uniform(0.25, 0.55)
        ndvi_after[patch] -= drop

    ndvi_after = np.clip(ndvi_after + rng.normal(0, 0.03, (h, w)), -1, 1).astype("float32")

    # Synthetic affine transform: ~10m pixels in a local projected-like
    # frame anchored at the AOI's lower-left corner (good enough for
    # area math without needing a real CRS for synthetic data).
    transform = rasterio.transform.from_origin(0, h * 10, 10, 10)

    return ndvi_before, ndvi_after, transform


def synthetic_change_result(spec: SyntheticEventSpec) -> dict:
    """Run the *real* threshold + vectorize logic against synthetic NDVI."""
    ndvi_before, ndvi_after, transform = generate_synthetic_ndvi_pair(spec.seed)
    delta = ndvi_after - ndvi_before

    threshold = settings.ndvi_change_threshold
    changed_mask = (np.abs(delta) >= threshold).astype("uint8")

    pixel_area_km2 = (10 * 10) / 1_000_000.0  # 10m x 10m pixels
    n_changed = int(changed_mask.sum())
    change_area_km2 = round(n_changed * pixel_area_km2, 4)

    severity_score = (
        round(float(np.clip(np.abs(delta[changed_mask == 1]).mean(), 0, 1)), 4)
        if n_changed > 0
        else 0.0
    )

    geojson = _vectorize_synthetic(changed_mask, delta, transform, spec.bbox, ndvi_before.shape)

    return {
        "tile_id": spec.tile_id,
        "date_before": spec.date_before,
        "date_after": spec.date_after,
        "bbox_geojson": geojson,
        "change_area_km2": change_area_km2,
        "severity_score": severity_score,
    }


def _vectorize_synthetic(
    mask: np.ndarray,
    delta: np.ndarray,
    transform: rasterio.Affine,
    bbox: list[float],
    shape: tuple[int, int],
) -> dict:
    """Vectorize the local-grid mask, then rescale geometries into the
    real WGS84 bbox so the polygons land in a real, mappable location."""
    h, w = shape
    min_lon, min_lat, max_lon, max_lat = bbox
    lon_scale = (max_lon - min_lon) / (w * 10)
    lat_scale = (max_lat - min_lat) / (h * 10)

    features = []
    for geom, value in rasterio_shapes(mask, mask=mask.astype(bool), transform=transform):
        if value != 1:
            continue
        poly = shapely_shape(geom)

        def to_wgs84(x, y, z=None):
            lon = min_lon + x * lon_scale
            lat = min_lat + y * lat_scale
            return (lon, lat)

        from shapely.ops import transform as shp_transform

        poly_wgs84 = shp_transform(to_wgs84, poly)

        minx, miny, maxx, maxy = poly.bounds
        inv = ~transform
        col_min, row_max = inv * (minx, miny)
        col_max, row_min = inv * (maxx, maxy)
        r0, c0 = max(int(row_min), 0), max(int(col_min), 0)
        r1, c1 = min(int(row_max) + 1, h), min(int(col_max) + 1, w)
        local_delta = delta[r0:r1, c0:c1]
        local_severity = float(np.abs(local_delta).mean()) if local_delta.size else 0.0

        features.append(
            {
                "type": "Feature",
                "geometry": poly_wgs84.__geo_interface__,
                "properties": {"severity": round(min(max(local_severity, 0.0), 1.0), 4)},
            }
        )

    return {"type": "FeatureCollection", "features": features}


def all_synthetic_events() -> list[dict]:
    return [synthetic_change_result(spec) for spec in DEMO_SCENARIOS]
