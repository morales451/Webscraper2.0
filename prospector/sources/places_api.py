"""Official Google Places API (New) source.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
A Text Search returns at most 20 results per page and 60 results per query (3 pages).
To cover more ground, search several locations (neighbourhoods, ZIP codes, nearby towns).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx

from .base import LogFn, Place, SourceError

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
MAX_RESULTS_PER_QUERY = 60

FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.types",
        "places.primaryTypeDisplayName",
        "places.googleMapsUri",
        "places.location",
        "places.businessStatus",
        "nextPageToken",
    ]
)


def place_from_api(item: dict, query: str) -> Place:
    types = [t.replace("_", " ") for t in item.get("types", []) if t not in ("point_of_interest", "establishment")]
    primary = (item.get("primaryTypeDisplayName") or {}).get("text") or (types[0].capitalize() if types else "")
    location = item.get("location") or {}
    return Place(
        name=(item.get("displayName") or {}).get("text", ""),
        place_id=item.get("id", ""),
        category=primary,
        categories=[primary, *[t for t in types if t.lower() != primary.lower()]] if primary else types,
        address=item.get("formattedAddress", ""),
        phone=item.get("internationalPhoneNumber") or item.get("nationalPhoneNumber") or "",
        website=item.get("websiteUri", ""),
        rating=item.get("rating"),
        reviews=item.get("userRatingCount"),
        maps_url=item.get("googleMapsUri", ""),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        business_status=item.get("businessStatus", ""),
        source="api",
        query=query,
    )


class GooglePlacesApiSource:
    name = "api"

    def __init__(
        self,
        api_key: str,
        language: str = "en",
        region: str | None = None,
        client: httpx.AsyncClient | None = None,
        log: LogFn | None = None,
    ) -> None:
        if not api_key:
            raise SourceError("A Google Places API key is required for the API source.")
        self.api_key = api_key
        self.language = language
        self.region = region
        self._client = client
        self._owns_client = client is None
        self.log = log or (lambda msg: None)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def _post(self, body: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }
        for attempt in range(3):
            resp = await self.client.post(TEXT_SEARCH_URL, json=body, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if resp.status_code != 200:
                try:
                    message = resp.json().get("error", {}).get("message", resp.text)
                except ValueError:
                    message = resp.text
                raise SourceError(f"Google Places API error {resp.status_code}: {message[:300]}")
            return resp.json()
        raise SourceError("Google Places API kept returning errors (rate limited?). Try again later.")

    async def search(self, query: str, limit: int) -> AsyncIterator[Place]:
        limit = min(limit, MAX_RESULTS_PER_QUERY)
        body: dict = {"textQuery": query, "pageSize": min(20, limit), "languageCode": self.language}
        if self.region:
            body["regionCode"] = self.region
        count = 0
        while count < limit:
            data = await self._post(body)
            for item in data.get("places", []):
                yield place_from_api(item, query)
                count += 1
                if count >= limit:
                    return
            token = data.get("nextPageToken")
            if not token:
                return
            body = {**body, "pageToken": token}

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
