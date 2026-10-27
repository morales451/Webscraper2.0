"""Common types for business-listing sources (Google Places API, Google Maps browser scraping)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Callable, Protocol


class SourceError(RuntimeError):
    """A listing source failed in a way the user should see (bad API key, blocked, ...)."""


@dataclass
class Place:
    name: str
    place_id: str = ""
    category: str = ""
    categories: list[str] = field(default_factory=list)
    address: str = ""
    phone: str = ""
    website: str = ""
    rating: float | None = None
    reviews: int | None = None
    maps_url: str = ""
    latitude: float | None = None
    longitude: float | None = None
    business_status: str = ""  # OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY / ""
    source: str = ""
    query: str = ""

    @property
    def key(self) -> str:
        """Stable identity used for de-duplication across searches and runs."""
        if self.place_id:
            return self.place_id
        base = f"{self.name}|{self.address}".lower()
        return "na:" + re.sub(r"[^a-z0-9|]+", "", base)

    def to_dict(self) -> dict:
        return asdict(self)


LogFn = Callable[[str], None]


class ListingSource(Protocol):
    name: str

    def search(self, query: str, limit: int) -> AsyncIterator[Place]:
        """Yield up to ``limit`` places for a free-text query like 'dentist in Austin, TX'."""
        ...

    async def close(self) -> None: ...


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_float(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(m.group(0).replace(",", ".")) if m else None
