"""
STAC catalog access for Sentinel-2 L2A scenes.

Primary catalog: Microsoft Planetary Computer (requires SAS signing of
asset URLs before rasterio can read the COGs).
Fallback catalog: Element84 Earth Search (public, no signing required,
same Sentinel-2 L2A collection - collection id differs slightly, see
EARTH_SEARCH_COLLECTION).

The fallback is used automatically when:
  - STAC_API_URL is explicitly set to the Earth Search endpoint, or
  - the Planetary Computer search raises an APIError / timeout (after
    STAC_RETRIES retries with exponential back-off).

This makes the script much more robust: Planetary Computer's search
endpoint occasionally times out under load, but Earth Search is usually
snappy.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
from pystac import Item
from pystac_client import Client
from pystac_client.exceptions import APIError

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# PC uses Sentinel band IDs (B04/B08); Earth Search uses common names (red/nir).
# Map internal key -> (PC asset name, Earth Search asset name).
NDVI_BAND_ASSETS: dict[str, tuple[str, str]] = {
    "B04": ("B04", "red"),
    "B08": ("B08", "nir"),
}
NDVI_BANDS = tuple(NDVI_BAND_ASSETS)  # internal keys: ("B04", "B08")

# Element84 Earth Search - public, no auth, no signing required.
EARTH_SEARCH_URL        = "https://earth-search.aws.element84.com/v1"
EARTH_SEARCH_COLLECTION = "sentinel-2-l2a"    # same name on ES v1
PC_URL                  = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_COLLECTION           = "sentinel-2-l2a"

STAC_RETRIES    = 3
STAC_TIMEOUT    = 60        # seconds per request
RETRY_BASE_WAIT = 3         # seconds; doubles each retry


@dataclass(frozen=True)
class SceneRef:
    item_id: str
    datetime_utc: datetime
    cloud_cover: float
    bbox: list[float]
    band_urls: dict[str, str]   # band name -> rasterio-readable HTTPS URL


def _is_earth_search(url: str) -> bool:
    return "element84" in url


class StacScenePicker:
    """Find a low-cloud Sentinel-2 scene pair over an AOI, N months apart."""

    def __init__(self, stac_api_url: str | None = None) -> None:
        self.stac_api_url = stac_api_url or settings.stac_api_url
        self._use_earth_search = _is_earth_search(self.stac_api_url)
        self._collection = (
            EARTH_SEARCH_COLLECTION if self._use_earth_search else PC_COLLECTION
        )
        logger.info("Opening STAC catalog at %s", self.stac_api_url)
        self._catalog = Client.open(
            self.stac_api_url,
            headers={"User-Agent": "satellite-change-detection/0.1"},
        )
        # Fallback catalog opened lazily if PC times out.
        self._fallback_catalog: Client | None = None

    # ------------------------------------------------------------------
    # Internal search helpers
    # ------------------------------------------------------------------

    def _open_fallback(self) -> None:
        if self._fallback_catalog is None:
            logger.warning(
                "Planetary Computer timed out; falling back to Earth Search (%s)",
                EARTH_SEARCH_URL,
            )
            self._fallback_catalog = Client.open(EARTH_SEARCH_URL)
            self._use_earth_search = True
            self._collection = EARTH_SEARCH_COLLECTION

    def _search_once(
        self,
        catalog: Client,
        collection: str,
        bbox: list[float],
        start: date,
        end: date,
        max_cloud: float,
        limit: int,
    ) -> list[Item]:
        search = catalog.search(
            collections=[collection],
            bbox=bbox,
            datetime=f"{start.isoformat()}/{end.isoformat()}",
            query={"eo:cloud_cover": {"lt": max_cloud}},
            limit=limit,
        )
        items = list(search.items())
        items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100.0))
        return items

    def _search(
        self,
        bbox: list[float],
        start: date,
        end: date,
        max_cloud: float,
        limit: int = 20,
    ) -> list[Item]:
        """Search with retry + automatic fallback to Earth Search on timeout."""
        last_exc: Exception | None = None

        for attempt in range(STAC_RETRIES):
            catalog  = self._fallback_catalog or self._catalog
            coll     = self._collection
            try:
                return self._search_once(catalog, coll, bbox, start, end, max_cloud, limit)
            except (APIError, Exception) as exc:
                last_exc = exc
                is_timeout = (
                    "maximum allowed time" in str(exc).lower()
                    or "timeout" in str(exc).lower()
                    or "timed out" in str(exc).lower()
                )
                # On timeout, immediately switch to Earth Search instead
                # of burning all retries against the same slow endpoint.
                if is_timeout and not self._use_earth_search:
                    self._open_fallback()
                    continue

                wait = RETRY_BASE_WAIT * (2 ** attempt)
                logger.warning(
                    "STAC search attempt %d/%d failed (%s); retrying in %ds",
                    attempt + 1, STAC_RETRIES, exc, wait,
                )
                time.sleep(wait)

        raise RuntimeError(
            f"STAC search failed after {STAC_RETRIES} attempts"
        ) from last_exc

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def find_scene_pair(
        self,
        bbox: list[float],
        months_apart: int = 6,
        anchor_date: date | None = None,
        max_cloud: float = 20.0,
        search_window_days: int = 30,
    ) -> tuple[Item, Item]:
        """
        Return (before_item, after_item): the lowest-cloud Sentinel-2 scenes
        found over `bbox` in two time windows roughly `months_apart` apart.
        """
        anchor       = anchor_date or date.today()
        after_target = anchor - timedelta(days=30)
        before_target = after_target - timedelta(days=months_apart * 30)

        def window(t: date) -> tuple[date, date]:
            return t - timedelta(days=search_window_days), t + timedelta(days=search_window_days)

        after_start,  after_end  = window(after_target)
        before_start, before_end = window(before_target)

        logger.info("Searching 'after'  window: %s – %s", after_start, after_end)
        after_items = self._search(bbox, after_start, after_end, max_cloud)

        logger.info("Searching 'before' window: %s – %s", before_start, before_end)
        before_items = self._search(bbox, before_start, before_end, max_cloud)

        if not after_items:
            raise ValueError(
                f"No Sentinel-2 scenes for bbox={bbox} in {after_start}–{after_end} "
                f"with cloud cover < {max_cloud}%"
            )
        if not before_items:
            raise ValueError(
                f"No Sentinel-2 scenes for bbox={bbox} in {before_start}–{before_end} "
                f"with cloud cover < {max_cloud}%"
            )

        return before_items[0], after_items[0]

    # ------------------------------------------------------------------
    # Asset URL resolution
    # ------------------------------------------------------------------

    def _resolve_url(self, href: str) -> str:
        """Return a rasterio-readable URL.

        Earth Search COGs are publicly accessible as-is.
        Planetary Computer COGs need a short-lived SAS token attached.
        """
        if self._use_earth_search:
            return href
        return self._sign_pc_url(href)

    @staticmethod
    def _sign_pc_url(url: str) -> str:
        """Attach a SAS token to a Planetary Computer asset URL."""
        for attempt in range(STAC_RETRIES):
            try:
                resp = httpx.get(
                    settings.pc_sign_url,
                    params={"href": url},
                    timeout=STAC_TIMEOUT,
                )
                resp.raise_for_status()
                return resp.json()["href"]
            except Exception as exc:
                if attempt == STAC_RETRIES - 1:
                    raise
                wait = RETRY_BASE_WAIT * (2 ** attempt)
                logger.warning("PC sign attempt %d failed (%s); retrying in %ds", attempt+1, exc, wait)
                time.sleep(wait)
        raise RuntimeError("PC URL signing failed")   # unreachable but satisfies mypy

    def to_scene_ref(self, item: Item) -> SceneRef:
        band_urls: dict[str, str] = {}
        for internal_key, (pc_name, es_name) in NDVI_BAND_ASSETS.items():
            # Pick the right asset name depending on which catalog served this item.
            asset_name = es_name if self._use_earth_search else pc_name
            asset = item.assets.get(asset_name)
            if asset is None:
                raise ValueError(
                    f"STAC item {item.id!r} has no asset {asset_name!r}. "
                    f"Available assets: {list(item.assets)}"
                )
            band_urls[internal_key] = self._resolve_url(asset.href)
            logger.info("Resolved %s (%s) for %s: %s…",
                        internal_key, asset_name, item.id, band_urls[internal_key][:80])

        return SceneRef(
            item_id=item.id,
            datetime_utc=item.datetime,
            cloud_cover=item.properties.get("eo:cloud_cover", float("nan")),
            bbox=item.bbox,
            band_urls=band_urls,
        )
