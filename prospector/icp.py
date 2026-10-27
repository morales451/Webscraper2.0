"""Ideal Customer Profiles (ICPs).

A profile describes who you want to find:
  * ``queries``    – what to type into Google Maps ("roofing contractor", "roofer")
  * ``locations``  – where ("Austin, TX", "78704", "Round Rock, TX")
  * ``keywords``   – words on a prospect's website that signal a good fit, with weights
  * ``required_keywords`` / ``exclude_keywords`` – hard qualify / disqualify rules
  * ``filters``    – rating, review count, category, website/phone/email requirements

Profiles live as YAML files in the ``profiles/`` folder so they're easy to copy, edit by
hand, or share. The dashboard edits the same files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_WEIGHTED_LINE = re.compile(r"^(.*?)\s*(?:\||=)\s*(-?\d+(?:\.\d+)?)\s*$")


class Keyword(BaseModel):
    term: str
    weight: float = 1.0

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        # Accept "solar", "solar | 3", "solar = 3", {"solar": 3} or {"term": "solar", "weight": 3}.
        if isinstance(value, str):
            m = _WEIGHTED_LINE.match(value.strip())
            if m and m.group(1).strip():
                return {"term": m.group(1).strip(), "weight": float(m.group(2))}
            return {"term": value.strip()}
        if isinstance(value, dict) and "term" not in value and len(value) == 1:
            (term, weight), = value.items()
            return {"term": str(term), "weight": weight if weight is not None else 1.0}
        return value

    @field_validator("term")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("keyword can't be empty")
        return v


class Filters(BaseModel):
    min_rating: float | None = Field(default=None, ge=0, le=5)
    min_reviews: int | None = Field(default=None, ge=0)
    max_reviews: int | None = Field(default=None, ge=0)
    require_website: bool = False
    require_phone: bool = False
    require_email: bool = False
    exclude_closed: bool = True
    # Substrings matched against the Google Maps category, e.g. ["dentist", "orthodontist"].
    include_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    # Substrings matched against the business name, e.g. chains you don't want: ["Aspen Dental"].
    exclude_names: list[str] = Field(default_factory=list)


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    out, seen = [], set()
    for v in values:
        s = str(v).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


class ICPProfile(BaseModel):
    name: str
    description: str = ""
    queries: list[str] = Field(min_length=1)
    locations: list[str] = Field(default_factory=list)
    keywords: list[Keyword] = Field(default_factory=list)
    required_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    filters: Filters = Field(default_factory=Filters)
    max_results_per_search: int = Field(default=60, ge=1, le=500)
    max_leads: int | None = Field(default=None, ge=1)
    pages_per_site: int = Field(default=5, ge=1, le=20)
    min_score: int = Field(default=0, ge=0, le=100)
    skip_seen: bool = True
    # Coverage: search each location as a grid of smaller areas (finds far more businesses).
    split_areas: bool = False
    max_areas: int = Field(default=9, ge=1, le=64)
    # After the planned searches, also search Google categories common among qualified leads.
    expand_queries: bool = False
    # Check that each email's domain can receive mail.
    check_emails: bool = True
    # One-line Claude note on why each qualified lead fits (needs an Anthropic API key).
    ai_notes: bool = False
    # Re-run automatically while the dashboard is open: "", "daily" or "weekly".
    schedule: Literal["", "daily", "weekly"] = ""

    @field_validator("queries", "locations", "required_keywords", "exclude_keywords", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)

    @field_validator("keywords", mode="before")
    @classmethod
    def _keywords(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            v = v.splitlines()
        if isinstance(v, dict):
            v = [{k: w} for k, w in v.items()]
        return [k for k in v if not (isinstance(k, str) and not k.strip())]

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("profile name can't be empty")
        return v

    def search_terms(self) -> list[str]:
        """Every query × location combination, e.g. 'roofer in Austin, TX'."""
        if not self.locations:
            return list(self.queries)
        return [f"{q} in {loc}" for loc in self.locations for q in self.queries]

    def to_yaml_dict(self) -> dict:
        data = self.model_dump(exclude_none=True)
        # Leave optional switches out of the file while they're at their defaults.
        for key, default in (("split_areas", False), ("max_areas", 9), ("expand_queries", False),
                             ("check_emails", True), ("ai_notes", False), ("schedule", "")):
            if data.get(key) == default:
                data.pop(key, None)
        data["keywords"] = [k.term if k.weight == 1 else {k.term: k.weight} for k in self.keywords]
        defaults = Filters().model_dump(exclude_none=True)
        data["filters"] = {k: v for k, v in data["filters"].items() if defaults.get(k, object()) != v}
        if not data["filters"]:
            data.pop("filters")
        return data


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "profile"


class ProfileStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, slug: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
            raise ValueError(f"invalid profile id {slug!r}")
        return self.directory / f"{slug}.yaml"

    def list(self) -> list[dict]:
        items = []
        for path in sorted(self.directory.glob("*.yaml")):
            try:
                profile = self.load_file(path)
            except Exception as exc:  # show broken files instead of hiding them
                items.append({"id": path.stem, "name": path.stem, "error": str(exc)})
                continue
            items.append({"id": path.stem, **profile.model_dump()})
        return items

    @staticmethod
    def load_file(path: Path) -> ICPProfile:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ICPProfile.model_validate(data)

    def get(self, slug: str) -> ICPProfile:
        path = self._path(slug)
        if not path.is_file():
            raise KeyError(slug)
        return self.load_file(path)

    def save(self, profile: ICPProfile, slug: str | None = None) -> str:
        slug = slug or slugify(profile.name)
        path = self._path(slug)
        text = yaml.safe_dump(profile.to_yaml_dict(), sort_keys=False, allow_unicode=True, width=100)
        path.write_text(text, encoding="utf-8")
        return slug

    def delete(self, slug: str) -> None:
        path = self._path(slug)
        if path.is_file():
            path.unlink()
