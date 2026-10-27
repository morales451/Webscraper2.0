"""Qualify and score a prospect against an ICP profile.

Score (0-100) = weight of the keywords found / weight of all keywords. A keyword counts once
no matter how many times it appears, so a site that mentions five of your signals beats one
that repeats a single word fifty times. Negative weights subtract.

Every match keeps a few text snippets and the page it came from, so you can see *why* a
prospect scored the way it did.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache

from .enrich.website import SiteData
from .icp import Filters, ICPProfile
from .sources.base import Place

SNIPPET_RADIUS = 70
MAX_SNIPPETS = 3


@lru_cache(maxsize=2048)
def compile_term(term: str) -> re.Pattern:
    """Whole-word, case-insensitive matcher. ``*`` is a wildcard: ``implant*`` matches
    'implant', 'implants', 'implantology'. Spaces match any whitespace."""
    parts = [re.escape(p) for p in term.strip().split()]
    body = r"\s+".join(parts).replace(r"\*", r"[\w-]*")
    return re.compile(rf"(?<![\w]){body}(?![\w])", re.I)


@dataclass
class KeywordMatch:
    term: str
    weight: float
    count: int = 0
    snippets: list[dict] = field(default_factory=list)


@dataclass
class Evaluation:
    score: int | None
    qualified: bool
    reasons: list[str] = field(default_factory=list)
    matches: list[KeywordMatch] = field(default_factory=list)
    required_found: list[str] = field(default_factory=list)
    excluded_found: list[str] = field(default_factory=list)

    def matches_dict(self) -> list[dict]:
        return [asdict(m) for m in self.matches]


def _snippet(text: str, start: int, end: int) -> str:
    a = max(0, start - SNIPPET_RADIUS)
    b = min(len(text), end + SNIPPET_RADIUS)
    s = text[a:b].strip()
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def _documents(place: Place, site: SiteData | None) -> list[tuple[str, str]]:
    """(where, text) pairs searched for keywords."""
    docs = [("Google Maps name", place.name), ("Google Maps category", " / ".join(place.categories or [place.category]))]
    if site is not None:
        for page in site.pages:
            docs.append((page.url, f"{page.title} {page.text}"))
    return [(w, t) for w, t in docs if t]


def find_term(term: str, docs: list[tuple[str, str]]) -> tuple[int, list[dict]]:
    rx = compile_term(term)
    count, snippets = 0, []
    for where, text in docs:
        for m in rx.finditer(text):
            count += 1
            if len(snippets) >= MAX_SNIPPETS or any(s["where"] == where for s in snippets):
                continue
            snippet = _snippet(text, m.start(), m.end())
            if not any(s["text"] == snippet for s in snippets):
                snippets.append({"where": where, "text": snippet})
    return count, snippets


def _any_contains(haystacks: list[str], needles: list[str]) -> str | None:
    for needle in needles:
        n = needle.lower()
        for h in haystacks:
            if n in (h or "").lower():
                return needle
    return None


def prefilter(place: Place, filters: Filters) -> list[str]:
    """Checks that only need Google Maps data (run before crawling the website)."""
    reasons: list[str] = []
    if filters.exclude_closed and place.business_status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
        reasons.append("business is closed")
    if filters.min_rating is not None and place.rating is not None and place.rating < filters.min_rating:
        reasons.append(f"rating {place.rating} < {filters.min_rating}")
    if filters.min_rating is not None and place.rating is None:
        reasons.append("no rating")
    if filters.min_reviews is not None and place.reviews is not None and place.reviews < filters.min_reviews:
        reasons.append(f"{place.reviews} reviews < {filters.min_reviews}")
    if filters.max_reviews is not None and place.reviews is not None and place.reviews > filters.max_reviews:
        reasons.append(f"{place.reviews} reviews > {filters.max_reviews}")
    if filters.require_website and not place.website:
        reasons.append("no website")
    if filters.require_phone and not place.phone:
        reasons.append("no phone")
    cats = place.categories or [place.category]
    if filters.include_categories and not _any_contains(cats, filters.include_categories):
        reasons.append(f"category '{place.category}' not in wanted categories")
    hit = _any_contains(cats, filters.exclude_categories)
    if hit:
        reasons.append(f"excluded category '{hit}'")
    hit = _any_contains([place.name], filters.exclude_names)
    if hit:
        reasons.append(f"excluded name '{hit}'")
    return reasons


def evaluate(place: Place, site: SiteData | None, profile: ICPProfile, pre_reasons: list[str] | None = None) -> Evaluation:
    docs = _documents(place, site)
    reasons = list(pre_reasons or [])

    matches: list[KeywordMatch] = []
    positive_total = sum(k.weight for k in profile.keywords if k.weight > 0)
    gained = 0.0
    for kw in profile.keywords:
        count, snippets = find_term(kw.term, docs)
        if count:
            matches.append(KeywordMatch(kw.term, kw.weight, count, snippets))
            gained += kw.weight
    score: int | None = None
    if profile.keywords and positive_total > 0:
        score = max(0, min(100, round(100 * gained / positive_total)))
    matches.sort(key=lambda m: (-m.weight, -m.count))

    required_found = [t for t in profile.required_keywords if find_term(t, docs)[0]]
    if profile.required_keywords and not required_found:
        reasons.append("none of the required keywords found")

    excluded_found = []
    for term in profile.exclude_keywords:
        count, snippets = find_term(term, docs)
        if count:
            excluded_found.append(term)
            matches.append(KeywordMatch(f"NOT: {term}", 0, count, snippets))
    if excluded_found:
        reasons.append("excluded keyword: " + ", ".join(excluded_found))

    if profile.filters.require_email and not (site and site.emails):
        reasons.append("no email found")
    if score is not None and profile.min_score and score < profile.min_score:
        reasons.append(f"score {score} < {profile.min_score}")

    return Evaluation(
        score=score,
        qualified=not reasons,
        reasons=reasons,
        matches=matches,
        required_found=required_found,
        excluded_found=excluded_found,
    )
