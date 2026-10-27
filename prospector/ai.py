"""Optional AI fit note: Claude reads what we found about a lead and writes one line on why it
does or doesn't fit your profile, plus its own 0-100 fit rating.

Off by default. Needs an Anthropic API key (Settings page, or ANTHROPIC_API_KEY). It runs only
on leads that already passed your filters, so you pay for the ones worth reading about.
"""

from __future__ import annotations

import asyncio
import json

import anthropic

from .enrich.website import SiteData
from .icp import ICPProfile
from .sources.base import Place

DEFAULT_MODEL = "claude-opus-5-5"
MAX_PAGE_CHARS = 2_500

SYSTEM_PROMPT = """You qualify sales prospects for a small business owner. You get their ideal \
customer profile (ICP) and what was found about one business: its Google Maps listing and text \
from its website. Judge how well the business fits the ICP based only on that evidence.

Write the note for the salesperson about to contact this business: one sentence, at most 30 \
words, naming the concrete evidence (services, specialties, size hints, tools they use) that \
makes them a good or poor fit. If the evidence is thin, say so rather than guessing."""

OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "fit_score": {"type": "integer", "description": "0 = clearly not a fit, 100 = ideal customer"},
            "note": {"type": "string"},
        },
        "required": ["fit_score", "note"],
        "additionalProperties": False,
    },
}


def build_prompt(profile: ICPProfile, place: Place, site: SiteData | None, matched: list[str]) -> str:
    keywords = ", ".join(k.term for k in profile.keywords) or "(none)"
    lines = [
        "<icp>",
        f"Name: {profile.name}",
        f"Description: {profile.description or '(none)'}",
        f"Searched for: {', '.join(profile.queries)}",
        f"Positive signals: {keywords}",
    ]
    if profile.exclude_keywords:
        lines.append(f"Disqualifiers: {', '.join(profile.exclude_keywords)}")
    lines += [
        "</icp>",
        "<business>",
        f"Name: {place.name}",
        f"Google category: {', '.join(place.categories or [place.category])}",
        f"Address: {place.address}",
        f"Rating: {place.rating} ({place.reviews if place.reviews is not None else 'unknown'} reviews)",
        f"Keywords found on site: {', '.join(matched) or 'none'}",
    ]
    if site is not None and site.ok:
        if site.signals:
            lines.append(f"Website signals: {', '.join(site.signals)}")
        for page in site.pages[:4]:
            text = " ".join(x for x in (page.headings, page.text) if x)[:MAX_PAGE_CHARS]
            lines.append(f'<page url="{page.url}" title="{page.title}">\n{text}\n</page>')
    else:
        lines.append(f"Website: not readable ({site.error if site else 'none'})")
    lines.append("</business>")
    return "\n".join(lines)


class FitNoter:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, concurrency: int = 4):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
        self.model = model or DEFAULT_MODEL
        self._sem = asyncio.Semaphore(concurrency)
        self.disabled_reason: str | None = None

    async def note(self, profile: ICPProfile, place: Place, site: SiteData | None, matched: list[str]) -> dict | None:
        """Return {"fit_score": int, "note": str}, or None if unavailable for this lead."""
        if self.disabled_reason:
            return None
        async with self._sem:
            try:
                response = await self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    # Low effort: a one-line judgement doesn't need deep reasoning.
                    output_config={"effort": "low", "format": OUTPUT_SCHEMA},
                    # If a safety classifier declines, retry on a fallback model automatically.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": build_prompt(profile, place, site, matched)}],
                )
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                self.disabled_reason = f"Anthropic API key rejected: {exc.message}"
                return None
            except anthropic.NotFoundError:
                self.disabled_reason = f"Model '{self.model}' not available for this API key"
                return None
            except anthropic.BadRequestError as exc:
                self.disabled_reason = f"AI note request rejected: {exc.message}"
                return None
            except (anthropic.APIStatusError, anthropic.APIConnectionError):
                return None  # transient for this lead (the SDK already retried); skip it
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
            return {"fit_score": max(0, min(100, int(data["fit_score"]))), "note": str(data["note"]).strip()}
        except (ValueError, KeyError, TypeError):
            return None

    async def close(self) -> None:
        await self.client.close()
