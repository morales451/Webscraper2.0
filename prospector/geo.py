"""Split a city or region into a grid of smaller areas.

Google Maps stops at roughly 100-120 results per search (the Places API at 60), so one search
for "dentist in Dallas, TX" misses most dentists in Dallas. Searching each grid cell
separately and merging the results finds far more — the same trick PhantomBuster uses.

Locations are geocoded with OpenStreetMap's Nominatim service (free, no key; max 1 request
per second, so results are cached in ``data/geocache.json``).
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Prospector/0.2 (local lead-research tool; https://github.com/morales451/Webscraper2.0)"
KM_PER_DEG_LAT = 110.574


@dataclass
class Area:
    """A rectangle to search (degrees)."""

    south: float
    west: float
    north: float
    east: float
    label: str = ""

    @property
    def center(self) -> tuple[float, float]:
        return (self.south + self.north) / 2, (self.west + self.east) / 2

    @property
    def width_km(self) -> float:
        lat = math.radians(self.center[0])
        return abs(self.east - self.west) * 111.320 * math.cos(lat)

    @property
    def height_km(self) -> float:
        return abs(self.north - self.south) * KM_PER_DEG_LAT

    def maps_zoom(self, viewport_px: int = 900) -> int:
        """Google Maps zoom level whose viewport roughly covers this area."""
        lat = math.radians(self.center[0])
        span_m = max(self.width_km, self.height_km, 0.5) * 1000
        zoom = math.log2(156543.03 * math.cos(lat) * viewport_px / span_m)
        return max(9, min(17, int(zoom)))


@dataclass
class Place:
    query: str
    display_name: str
    area: Area


class Geocoder:
    def __init__(self, cache_file: Path | None = None, client: httpx.AsyncClient | None = None):
        self.cache_file = cache_file
        self._client = client
        self._owns = client is None
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._cache: dict[str, dict] = {}
        if cache_file and cache_file.is_file():
            try:
                self._cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT})
        return self._client

    async def geocode(self, location: str) -> Place | None:
        key = location.strip().lower()
        if key in self._cache:
            c = self._cache[key]
            return Place(location, c["display_name"], Area(**c["area"])) if c else None
        async with self._lock:  # Nominatim's usage policy: at most one request per second
            wait = 1.1 - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = await self.client.get(
                    NOMINATIM_URL,
                    params={"q": location, "format": "json", "limit": 1},
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                )
                resp.raise_for_status()
                results = resp.json()
            except (httpx.HTTPError, ValueError):
                return None  # network problem: don't cache
        if not results:
            self._remember(key, None)
            return None
        r = results[0]
        south, north, west, east = (float(x) for x in r["boundingbox"])
        place = Place(location, r.get("display_name", location), Area(south, west, north, east, location))
        self._remember(key, {"display_name": place.display_name, "area": asdict(place.area)})
        return place

    def _remember(self, key: str, value: dict | None) -> None:
        self._cache[key] = value
        if self.cache_file:
            try:
                self.cache_file.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")
            except OSError:
                pass

    async def close(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None


def split_area(area: Area, max_cells: int = 9, min_cell_km: float = 3.0) -> list[Area]:
    """Cut ``area`` into at most ``max_cells`` roughly square cells (never smaller than
    ``min_cell_km`` across). Small towns stay a single cell."""
    w, h = max(area.width_km, 0.1), max(area.height_km, 0.1)
    max_cells = max(1, max_cells)
    # Largest grid (cols x rows) that respects max_cells and min_cell_km, keeping cells square-ish.
    best = (1, 1)
    for cols in range(1, max_cells + 1):
        rows = max(1, round(cols * h / w))
        if cols * rows > max_cells:
            continue
        if w / cols < min_cell_km or h / rows < min_cell_km:
            continue
        if cols * rows > best[0] * best[1]:
            best = (cols, rows)
    cols, rows = best
    dlat = (area.north - area.south) / rows
    dlng = (area.east - area.west) / cols
    cells = []
    for r in range(rows):
        for c in range(cols):
            n = r * cols + c + 1
            cells.append(Area(
                area.south + r * dlat, area.west + c * dlng,
                area.south + (r + 1) * dlat, area.west + (c + 1) * dlng,
                f"{area.label} — area {n}/{rows * cols}" if rows * cols > 1 else area.label,
            ))
    return cells
