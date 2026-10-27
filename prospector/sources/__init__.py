"""Business-listing sources and a factory that picks one from settings."""

from __future__ import annotations

import asyncio

from ..config import Settings
from .base import ListingSource, LogFn, Place, SourceError
from .maps_browser import GoogleMapsBrowserSource
from .places_api import GooglePlacesApiSource


def make_source(
    settings: Settings,
    requested: str | None = None,
    log: LogFn | None = None,
    cancel: asyncio.Event | None = None,
    details: bool = True,
) -> ListingSource:
    kind = settings.resolved_source(requested)
    if kind == "api":
        if not settings.places_api_key:
            raise SourceError("The 'api' source needs a Google Places API key (Settings page or GOOGLE_PLACES_API_KEY).")
        return GooglePlacesApiSource(settings.places_api_key, log=log)
    return GoogleMapsBrowserSource(
        headless=settings.headless,
        details=details,
        chromium_path=settings.chromium_path,
        user_data_dir=settings.browser_profile_dir,
        log=log,
        cancel=cancel,
    )


__all__ = [
    "GoogleMapsBrowserSource",
    "GooglePlacesApiSource",
    "ListingSource",
    "Place",
    "SourceError",
    "make_source",
]
