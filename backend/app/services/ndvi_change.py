"""
Core change-detection math: read COG bands -> NDVI -> NDVI delta ->
threshold -> vectorize to GeoJSON polygons + summary stats.

Kept deliberately simple per the brief: a fixed +/- threshold on NDVI
delta, no atmospheric correction beyond what's baked into L2A surface
reflectance, no cloud masking beyond the STAC-level eo:cloud_cover filter
used when picking scenes. Good enough to demonstrate the pipeline end to
end; a production system (closer to what LiveEO would run) would add
per-pixel cloud/shadow masking, terrain correction, and a more principled
change model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import rasterio
from rasterio.features import shapes as rasterio_shapes
from rasterio.warp import reproject, Resampling
from rasterio.windows import from_bounds
from shapely.geometry import shape as shapely_shape
from shapely.ops import transform as shapely_transform
import pyproj

from app.core.config import get_settings
from app.services.stac_client import SceneRef

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class ChangeResult:
    change_area_km2: float
    severity_score: float
    geojson: dict  # FeatureCollection, polygons in EPSG:4326


def _read_band_window(url: str, bbox_wgs84: list[float]) -> tuple[np.ndarray, rasterio.Affine, str]:
    """Read a single band, windowed to bbox_wgs84, reprojected to WGS84.

    Returns (array, transform, crs) all already in EPSG:4326 so the two
    scenes (which may be on slightly different UTM grids/sub-pixel grids)
    can be compared cell-for-cell after a final resample-to-common-grid
    step done by the caller.
    """
    with rasterio.open(url) as src:
        # Reproject bbox (WGS84) into the source's native CRS to compute
        # the read window in pixel space.
        transformer = pyproj.Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        min_x, min_y = transformer.transform(bbox_wgs84[0], bbox_wgs84[1])
        max_x, max_y = transformer.transform(bbox_wgs84[2], bbox_wgs84[3])
        window = from_bounds(min_x, min_y, max_x, max_y, transform=src.transform)
        data = src.read(1, window=window).astype("float32")
        win_transform = src.window_transform(window)
        return data, win_transform, src.crs.to_string()


def _ndvi_from_scene(scene: SceneRef, bbox_wgs84: list[float]) -> tuple[np.ndarray, rasterio.Affine, str]:
    red, transform, crs = _read_band_window(scene.band_urls["B04"], bbox_wgs84)
    nir, _, _ = _read_band_window(scene.band_urls["B08"], bbox_wgs84)

    # Sentinel-2 L2A reflectance is scaled by 10000.
    red = red / 10000.0
    nir = nir / 10000.0

    denom = nir + red
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = np.where(denom > 0, (nir - red) / denom, np.nan)

    return ndvi.astype("float32"), transform, crs


def _reproject_to_match(
    src_arr: np.ndarray, src_transform: rasterio.Affine, src_crs: str,
    dst_transform: rasterio.Affine, dst_crs: str, dst_shape: tuple[int, int],
) -> np.ndarray:
    dst_arr = np.full(dst_shape, np.nan, dtype="float32")
    reproject(
        source=src_arr,
        destination=dst_arr,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=Resampling.bilinear,
        src_nodata=np.nan,
        dst_nodata=np.nan,
    )
    return dst_arr


def _pixel_area_km2(transform: rasterio.Affine, crs: str) -> float:
    """Approximate per-pixel area in km^2 from the affine transform."""
    px_w = abs(transform.a)
    px_h = abs(transform.e)
    if pyproj.CRS(crs).is_geographic:
        # Degrees -> rough meters at the AOI's latitude (good enough for
        # a ~10m-resolution Sentinel-2 product reprojected to WGS84).
        meters_per_deg_lat = 111_320.0
        px_w_m = px_w * meters_per_deg_lat
        px_h_m = px_h * meters_per_deg_lat
    else:
        px_w_m, px_h_m = px_w, px_h
    return (px_w_m * px_h_m) / 1_000_000.0


def compute_change(
    before: SceneRef,
    after: SceneRef,
    bbox_wgs84: list[float],
    threshold: float | None = None,
) -> ChangeResult:
    """
    Compute NDVI for both scenes over bbox_wgs84, take the delta, threshold
    it, and vectorize the changed pixels into a GeoJSON FeatureCollection.

    Each output polygon carries a `severity` property (mean |delta NDVI|
    within that polygon) so the frontend can colour-code by severity.
    """
    threshold = threshold if threshold is not None else settings.ndvi_change_threshold

    ndvi_before, t_before, crs_before = _ndvi_from_scene(before, bbox_wgs84)
    ndvi_after, t_after, crs_after = _ndvi_from_scene(after, bbox_wgs84)

    # Resample "after" onto the "before" grid so we can diff cell-for-cell.
    ndvi_after_matched = _reproject_to_match(
        ndvi_after, t_after, crs_after, t_before, crs_before, ndvi_before.shape
    )

    delta = ndvi_after_matched - ndvi_before  # negative = vegetation loss
    valid = ~np.isnan(delta)

    changed_mask = np.zeros_like(delta, dtype="uint8")
    changed_mask[valid & (np.abs(delta) >= threshold)] = 1

    pixel_area_km2 = _pixel_area_km2(t_before, crs_before)
    n_changed = int(changed_mask.sum())
    change_area_km2 = round(n_changed * pixel_area_km2, 4)

    if n_changed > 0:
        severity_score = float(np.clip(np.nanmean(np.abs(delta[changed_mask == 1])), 0.0, 1.0))
    else:
        severity_score = 0.0

    geojson = _vectorize(changed_mask, delta, t_before, crs_before)

    return ChangeResult(
        change_area_km2=change_area_km2,
        severity_score=round(severity_score, 4),
        geojson=geojson,
    )


def _vectorize(mask: np.ndarray, delta: np.ndarray, transform: rasterio.Affine, crs: str) -> dict:
    """Turn the binary change mask into a GeoJSON FeatureCollection (EPSG:4326)."""
    project_to_4326 = None
    if pyproj.CRS(crs).to_epsg() != 4326:
        transformer = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        project_to_4326 = lambda x, y: transformer.transform(x, y)  # noqa: E731

    features = []
    for geom, value in rasterio_shapes(mask, mask=mask.astype(bool), transform=transform):
        if value != 1:
            continue
        poly_native = shapely_shape(geom)  # still in the source (UTM) CRS

        # Mean |delta NDVI| under this specific polygon, used to colour
        # individual features by local severity rather than just the
        # whole-scene average. Must use the native-CRS polygon since
        # `transform` (and therefore the pixel window) is in that CRS too.
        row_min, col_min, row_max, col_max = _poly_bounds_to_window(poly_native, transform)
        local_delta = delta[row_min:row_max, col_min:col_max]
        local_severity = float(np.nanmean(np.abs(local_delta))) if local_delta.size else 0.0

        poly = shapely_transform(project_to_4326, poly_native) if project_to_4326 else poly_native

        features.append(
            {
                "type": "Feature",
                "geometry": poly.__geo_interface__,
                "properties": {"severity": round(min(max(local_severity, 0.0), 1.0), 4)},
            }
        )

    return {"type": "FeatureCollection", "features": features}


def _poly_bounds_to_window(poly, transform: rasterio.Affine) -> tuple[int, int, int, int]:
    minx, miny, maxx, maxy = poly.bounds
    inv = ~transform
    col_min, row_max = inv * (minx, miny)
    col_max, row_min = inv * (maxx, maxy)
    return int(row_min), int(col_min), int(row_max) + 1, int(col_max) + 1
