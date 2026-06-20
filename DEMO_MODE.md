# Demo mode — what it is and how to turn it off

To make the project runnable and demoable immediately, `docker compose
up` defaults to `DEMO_MODE=true`, which seeds the database with
synthetic-but-realistic change events on first boot instead of running
the live pipeline.

## What's real and what's synthetic in demo mode

Real and unchanged by demo mode:
- The FastAPI app, routes, Pydantic schemas, and SQLAlchemy models
- The `change_events` Postgres schema (exactly as specified in the
  brief)
- The React/TypeScript/MapLibre frontend, including the severity color
  scale, sidebar, tile selector, and map interactions
- The Docker Compose + Nginx topology
- The NDVI/threshold/vectorize **algorithm** itself
  (`backend/app/services/ndvi_change.py`): reads two COG bands per
  scene, computes NDVI, reprojects the second scene onto the first
  scene's grid, takes the delta, thresholds at `NDVI_CHANGE_THRESHOLD`
  (default 0.15), and vectorizes the changed pixels into a GeoJSON
  FeatureCollection with a per-polygon severity score.
- The STAC client (`backend/app/services/stac_client.py`): queries
  Microsoft Planetary Computer for Sentinel-2 L2A scenes over a bbox,
  picks the lowest-cloud scene in each of two time windows roughly N
  months apart, and signs the COG asset URLs via PC's SAS endpoint.

Synthetic, demo-mode-only (`backend/app/services/demo_data.py`):
- The NDVI arrays themselves. Instead of reading real Sentinel-2 COGs,
  `generate_synthetic_ndvi_pair()` generates a plausible "before" NDVI
  surface (~0.55-0.75, typical of healthy cropland) and an "after"
  surface with 2-4 patches dropped by 0.25-0.55 to simulate harvest or
  vegetation stress.
- The exact dates and area names in the seeded events are placeholders
  attached to a real, documented tile (33UXP — the agricultural
  corridor between Vienna and Bratislava; verified via a published
  research figure, see below), not the output of an actual STAC query.

**What this means concretely: every area-in-km² and severity number you
see in demo mode is synthetic.** It runs through the same threshold +
polygon-extraction code a real run would use, so it's a faithful
preview of what the output looks like but it is not a real
measurement.

## Why tile 33UXP

33UXP was chosen because it's a documented, real Sentinel-2 MGRS tile
covering agricultural land between Vienna and Bratislava — confirmed by
the figure caption in Vuolo, Żółtak, Pipitone et al., "Data Service
Platform for Sentinel-2 Surface Reflectance and Value-Added Products"
(2016), which explicitly states tile 33UXP covers that corridor. The
same research group's other work names "Marchfeld" (a real agricultural
region northeast of Vienna, inside this tile) as a validation site for
Sentinel-2 vegetation studies, which is why the demo scenario labels
reference Marchfeld.

## How to switch to live mode and get a real finding

You'll need internet access to `planetarycomputer.microsoft.com`.

1. **Stop demo seeding.** In `docker-compose.yml`, set the backend's
   `DEMO_MODE` environment variable to `"false"`. This also disables the
   `POST /api/changes/detect` guard that otherwise returns a 503.

2. **Clear the synthetic rows** (optional, if you've already run
   `docker compose up` once in demo mode):
   ```bash
   docker compose exec db psql -U postgres -d changedetection \
     -c "TRUNCATE change_events RESTART IDENTITY;"
   ```

3. **Run the real pipeline.** Either:
   - Call the API: `POST http://localhost:8080/api/changes/detect`
     with a JSON body like
     `{"tile_id": "33UXP", "bbox": [16.85, 48.05, 17.15, 48.25], "months_apart": 6}`
     (all fields optional - it falls back to the defaults in
     `app/core/config.py`), or
   - Run the standalone script directly:
     ```bash
     cd backend
     pip install -r requirements.txt
     python scripts/run_pipeline.py
     ```

4. **Re-run unit tests against the real dependencies.** Run `pytest`:
   ```bash
   cd backend && pip install -r requirements.txt && pytest -v
   ```