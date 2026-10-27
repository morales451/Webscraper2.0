"""Qualify and score a prospect against an ICP profile.

Each keyword earns *credit* between 0 and 1 depending on how strongly the business shows it:

  * where it appears — Google Maps name/category, a page title, heading or meta description,
    or a detected website signal is strong (0.9); ordinary page text is solid (0.7); text that
    only appears in the navigation menu or footer is weak (0.4)
  * how widely — +0.1 for each extra page that mentions it (up to +0.2), +0.1 for 5+ mentions

Score (0-100) = Σ(weight × credit) / Σ(weight). So a site that features five of your signals
in its headings beats one whose menu merely links to them. Negative weights subtract.

A keyword can list synonyms separated by " / " — ``TPO / single-ply / membrane roof*`` counts
as one signal, matched by any of its forms.

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

STRONG, BODY, CHROME = "strong", "body", "chrome"
ZONE_CREDIT = {STRONG: 0.9, BODY: 0.7, CHROME: 0.4}
ZONE_LABEL = {STRONG: "featured", BODY: "page text", CHROME: "menu/footer only"}
SYNONYM_SEP = re.compile(r"\s+/\s+")


def split_synonyms(term: str) -> list[str]:
    return [t for t in SYNONYM_SEP.split(term.strip()) if t]


@lru_cache(maxsize=2048)
def compile_term(term: str) -> re.Pattern:
    """Whole-word, case-insensitive matcher. ``*`` is a wildcard: ``implant*`` matches
    'implant', 'implants', 'implantology'. Spaces match any whitespace. ``a / b`` matches
    either form."""
    alternatives = []
    for alt in split_synonyms(term) or [term]:
        parts = [re.escape(p) for p in alt.split()]
        alternatives.append(r"\s+".join(parts).replace(r"\*", r"[\w-]*"))
    body = "|".join(f"(?:{a})" for a in alternatives)
    return re.compile(rf"(?<![\w])(?:{body})(?![\w])", re.I)


@dataclass
class KeywordMatch:
    term: str
    weight: float
    count: int = 0
    snippets: list[dict] = field(default_factory=list)
    credit: float = 0.0
    pages: int = 0
    zone: str = ""


@dataclass
class Doc:
    where: str  # page URL or "Google Maps name" etc.
    zone: str   # STRONG / BODY / CHROME
    text: str
    page: int | None = None  # page index, for counting how many pages mention a term


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


def _documents(place: Place, site: SiteData | None) -> list[Doc]:
    """Every piece of text searched for keywords, tagged with how prominent it is."""
    docs = [
        Doc("Google Maps name", STRONG, place.name),
        Doc("Google Maps category", STRONG, " / ".join(place.categories or [place.category])),
    ]
    if site is not None:
        if site.signals:
            docs.append(Doc("Website signals", STRONG, " · ".join(site.signals)))
        for i, page in enumerate(site.pages):
            prominent = " · ".join(x for x in (page.title, page.description, page.headings) if x)
            docs.append(Doc(page.url, STRONG, prominent, i))
            docs.append(Doc(page.url, BODY, page.text, i))
            docs.append(Doc(page.url, CHROME, page.chrome, i))
    return [d for d in docs if d.text]


def find_term(term: str, docs: list[Doc]) -> KeywordMatch:
    rx = compile_term(term)
    match = KeywordMatch(term, 1.0)
    pages: set[int] = set()
    best = None
    for doc in docs:
        hits = list(rx.finditer(doc.text))
        if not hits:
            continue
        match.count += len(hits)
        if doc.page is not None:
            pages.add(doc.page)
        if best is None or ZONE_CREDIT[doc.zone] > ZONE_CREDIT[best]:
            best = doc.zone
        if len(match.snippets) >= MAX_SNIPPETS or any(s["where"] == doc.where for s in match.snippets):
            continue
        snippet = _snippet(doc.text, hits[0].start(), hits[0].end())
        if not any(s["text"] == snippet for s in match.snippets):
            match.snippets.append({"where": doc.where, "zone": ZONE_LABEL[doc.zone], "text": snippet})
    if best is not None:
        credit = ZONE_CREDIT[best] + 0.1 * min(max(len(pages) - 1, 0), 2) + (0.1 if match.count >= 5 else 0)
        match.credit = round(min(credit, 1.0), 2)
        match.pages = len(pages)
        match.zone = ZONE_LABEL[best]
    return match


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
        m = find_term(kw.term, docs)
        if m.count:
            m.weight = kw.weight
            matches.append(m)
            gained += kw.weight * m.credit
    score: int | None = None
    if profile.keywords and positive_total > 0:
        score = max(0, min(100, round(100 * gained / positive_total)))
    matches.sort(key=lambda m: (-m.weight * m.credit, -m.count))

    required_found = [t for t in profile.required_keywords if find_term(t, docs).count]
    if profile.required_keywords and not required_found:
        reasons.append("none of the required keywords found")

    excluded_found = []
    for term in profile.exclude_keywords:
        m = find_term(term, docs)
        if m.count:
            excluded_found.append(term)
            m.term, m.weight = f"NOT: {term}", 0
            matches.append(m)
    if excluded_found:
        reasons.append("excluded keyword: " + ", ".join(excluded_found))

    deliverable = [e for e in (site.emails if site else []) if site.email_checks.get(e) != "invalid"]
    if profile.filters.require_email and not deliverable:
        reasons.append("no email found" if not (site and site.emails) else "no deliverable email")
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
