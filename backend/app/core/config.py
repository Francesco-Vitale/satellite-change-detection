"""
Central application configuration.

All values are overridable via environment variables (see .env.example and
docker-compose.yml). Defaults point at a real, public Sentinel-2 tile
(33UXP, the Vienna-Bratislava agricultural corridor) so `docker compose up`
produces a meaningful result with zero required input from the user.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@db:5432/changedetection"
    )
    sync_database_url: str = (
        "postgresql+psycopg2://postgres:postgres@db:5432/changedetection"
    )

    # --- STAC catalog -----------------------------------------------------
    # Set STAC_CATALOG=earth_search to bypass Planetary Computer entirely
    # and use Element84 Earth Search (public, no SAS signing, often faster).
    # The stac_client auto-falls back to Earth Search on PC timeout anyway,
    # but setting this skips PC from the start.
    stac_catalog: str = "planetary_computer"  # or "earth_search"
    stac_collection: str = "sentinel-2-l2a"

    # PC signs asset URLs via this endpoint (needed for COG reads).
    pc_sign_url: str = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

    @property
    def stac_api_url(self) -> str:
        if self.stac_catalog == "earth_search":
            return "https://earth-search.aws.element84.com/v1"
        return "https://planetarycomputer.microsoft.com/api/stac/v1"

    # --- Default AOI ------------------------------------------------------
    default_mgrs_tile: str = "33UXP"
    # Agricultural sub-area inside tile 33UXP, between Vienna and Bratislava.
    default_bbox: list[float] = [16.85, 48.05, 17.15, 48.25]

    # --- Change detection -------------------------------------------------
    ndvi_change_threshold: float = 0.15   # |delta NDVI| >= this -> "changed"
    cloud_cover_max: float = 20.0         # max eo:cloud_cover % for scene search

    # --- App --------------------------------------------------------------
    api_v1_prefix: str = "/api"
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]
    demo_mode: bool = True  # see DEMO_MODE.md


@lru_cache
def get_settings() -> Settings:
    return Settings()
