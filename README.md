# Sentinel Watch — Satellite Change Detection

A small, full-stack pipeline that detects vegetation/land-cover change
between two Sentinel-2 scenes and shows it on a map: STAC catalog query
→ NDVI computation → thresholded change mask → PostgreSQL → React +
MapLibre frontend.

## What it does

1. Queries a public STAC catalog (Microsoft Planetary Computer) for two
   Sentinel-2 L2A scenes over the same tile, roughly six months apart,
   picking the clearest (lowest cloud cover) scene in each time window.
2. Reads the red and near-infrared bands from each scene's Cloud
   Optimized GeoTIFFs (COGs) with rasterio, computes NDVI for both
   dates, reprojects them onto a common grid, and takes the difference.
3. Thresholds the NDVI delta (±0.15 by default) to flag changed pixels,
   then vectorizes the result into GeoJSON polygons, each carrying a
   severity score (mean absolute NDVI delta within that polygon).
4. Stores each detection run as a row in Postgres: tile id, date pair,
   polygon geometry, total changed area in km², and severity.
5. Serves it through a FastAPI backend and a MapLibre-based React
   frontend — a dark, sensor-readout-styled map with change polygons
   color-coded green → amber → red by severity, plus a sidebar ranking
   events and a tile selector.

## Stack

| Layer | Choice |
|---|---|
| Backend | Python, FastAPI (async), httpx, pystac-client, rasterio |
| Frontend | React 18, TypeScript, Vite, MapLibre GL JS |
| Database | PostgreSQL |
| Catalog | Microsoft Planetary Computer STAC API |
| Deployment | Docker Compose (backend, frontend, Postgres, Nginx) |

## Architecture

```
                        ┌──────────────┐
   browser  ───────────▶│    Nginx     │  :8080
                        └──────┬───────┘
                   ┌───────────┴───────────┐
                   ▼                       ▼
           ┌───────────────┐       ┌───────────────┐
           │   frontend     │       │    backend     │
           │ React+MapLibre │       │    FastAPI     │
           │     :5173      │       │     :8000      │
           └───────────────┘       └───────┬────────┘
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                                   ▼
                  ┌───────────────┐                 ┌──────────────────┐
                  │   PostgreSQL   │                 │  Planetary        │
                  │  change_events │                 │  Computer STAC    │
                  │     :5432      │                 │  (live mode only) │
                  └───────────────┘                 └──────────────────┘
```

## Running it

Requires Docker and Docker Compose.

```bash
git clone <this repo>
cd satellite-change-detection
docker compose up --build
```

Then open **http://localhost:8080**. On first boot the backend seeds
synthetic-but-realistic demo change events (see DEMO_MODE.md) so there's
something on the map immediately — no API keys or further setup needed.

Other endpoints once running:
- Frontend direct (no Nginx): http://localhost:5173
- Backend API docs (Swagger UI): http://localhost:8000/docs
- Backend health check: http://localhost:8000/api/health

To switch from demo data to a real detection run against live Sentinel-2
imagery, see **[DEMO_MODE.md](./DEMO_MODE.md)** — it's a one
environment-variable change plus either an API call or a one-line
script.

## API

The frontend only calls two read endpoints, per the brief:

- `GET /api/changes` — list of all change events (id, tile, dates, area,
  severity), used by the sidebar.
- `GET /api/changes/{id}` — full detail for one event, including its
  GeoJSON polygons.

Two extra endpoints exist beyond the brief's minimum:
- `GET /api/changes/geojson` — every event's polygons in one
  FeatureCollection, tagged with `event_id`/`severity`, so the map can
  fetch and render everything in a single request instead of N calls.
- `POST /api/changes/detect` — triggers a real detection run against
  the live STAC catalog (disabled while `DEMO_MODE=true`; see
  DEMO_MODE.md).

## Database schema

```sql
CREATE TABLE change_events (
    id               SERIAL PRIMARY KEY,
    tile_id          VARCHAR(16) NOT NULL,
    date_before      DATE NOT NULL,
    date_after       DATE NOT NULL,
    bbox_geojson     JSONB NOT NULL,         -- FeatureCollection of change polygons
    change_area_km2  DOUBLE PRECISION NOT NULL,
    severity_score   DOUBLE PRECISION NOT NULL,  -- mean |Δ NDVI| in changed pixels, 0-1
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

(`bbox_geojson` holds the changed-pixel polygon set rather than just a
bounding box — the name follows the brief's spec verbatim.)

## Project layout

```
backend/
  app/
    api/routes.py          # GET /changes, GET /changes/{id}, POST /changes/detect
    core/config.py         # settings, defaults (tile, bbox, threshold)
    services/
      stac_client.py       # PySTAC + pystac-client: scene search + SAS signing
      ndvi_change.py        # rasterio: NDVI, reprojection, threshold, vectorize
      demo_data.py          # synthetic NDVI generator for demo mode (see DEMO_MODE.md)
    models/change_event.py # SQLAlchemy ORM model
    schemas/                # Pydantic request/response shapes
  scripts/run_pipeline.py  # standalone script to run the real pipeline once
  tests/                    # pytest unit tests for the change-detection math
frontend/
  src/
    components/             # MapView (MapLibre), Sidebar, TopBar
    api/client.ts            # fetch wrappers for the two main endpoints
    utils/severity.ts        # shared severity → color mapping
db/init.sql                  # schema reference (SQLAlchemy creates it at runtime)
nginx/nginx.conf              # reverse proxy: / → frontend, /api/ → backend
docker-compose.yml
DEMO_MODE.md                  # what's real vs synthetic, how to go live
```

## Design notes

- **Tile 33UXP** (the default AOI) is a real, documented Sentinel-2 MGRS
  tile covering agricultural land in the Marchfeld region between
  Vienna and Bratislava — chosen because it's a known, citable
  agricultural area well suited to seasonal NDVI change detection, not
  an arbitrary placeholder.
- **Change detection is intentionally simple**: a fixed ±0.15 NDVI delta
  threshold, no cloud/shadow masking beyond the STAC-level
  `eo:cloud_cover` filter used when picking scenes, no terrain
  correction. That's a deliberate scope choice for a portfolio piece —
  a production system would add per-pixel cloud masking, multi-temporal
  compositing to reduce noise, and probably a model-based rather than
  fixed-threshold change definition.
- **Severity score** is the mean absolute NDVI delta within the changed
  pixels of a given polygon (or scene, for the whole-event score),
  normalized 0–1, matching the brief's definition exactly.

## Future development

- Per-pixel cloud/shadow masking using the Sentinel-2 scene
  classification layer (SCL band), rather than relying solely on
  scene-level cloud cover percentage.
- A time series view rather than a single before/after pair, to
  distinguish a one-off harvest cycle from a persistent land-cover
  change.
- Authentication and rate limiting on `POST /api/changes/detect` before
  exposing it beyond local/demo use.
