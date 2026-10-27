"""Suggest new keywords after a run: phrases that show up on many of your *best* leads'
websites but rarely on the rest. Click one in the dashboard to add it to the profile."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .scoring import compile_term

MAX_CHARS_PER_LEAD = 40_000
MAX_NGRAMS_PER_LEAD = 6_000

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just let me more
most my myself no nor not now of off on once only or other our ours ourselves out over own same she
should so some such than that the their theirs them themselves then there these they this those
through to too under until up very was we were what when where which while who whom why will with
would you your yours yourself yourselves us get got one two three new best top home page contact
call today click here read learn view see find info information website site menu skip content
copyright rights reserved privacy policy terms use cookies cookie email phone address hours open
monday tuesday wednesday thursday friday saturday sunday am pm inc llc co com www http https
that's don't we're it's you're we've we'll you'll can't won't isn't let's there's here's what's
just why choose word take
""".split())

# Everyday website copy that says nothing about what a business actually does.
GENERIC = set("""
ensure everyone helps help helping recommend natural deliver delivers delivering handles handle extend
assistance minimize specialized expertise expert experts book booking every day feel comfortable
comfortable quality service services team staff professional professionals experience experienced
years year customer customers client clients care caring trusted trust family friendly local area
areas community serving serve provide provides providing offer offers offering available today
work working job jobs great best high highly make makes making need needs needed time times know
like look looking want way well also many much more most first free call contact visit learn
schedule appointment appointments office offices location locations hours request estimate estimates
patients patient people person doctors doctor dr process project projects solutions solution
company companies business businesses owned operated licensed insured satisfaction guaranteed
guarantee reviews review rated award winning affordable fast reliable dedicated committed
building buildings home homes property properties new old small large right left full complete
everything anything something things thing whether including include includes different
across discuss taking take takes identify latest confidence protect protecting communities meet led
based throughout within around near nearby proudly proud since founded welcome visit thank thanks
""".split())

_WORD = re.compile(r"[a-z][a-z'&-]*[a-z]|[a-z]")


def _ngrams(text: str, limit: int) -> set[str]:
    words = [w for w in _WORD.findall(text.lower().replace("\u2019", "'")) if len(w) > 1]
    out: set[str] = set()
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            if n == 1 and len(gram[0]) < 4:
                continue
            out.add(" ".join(gram))
            if len(out) >= limit:
                return out
    return out


@dataclass
class SuggestionCollector:
    """Collects each lead's phrases during a run; ``suggest()`` ranks them at the end."""

    known_terms: list[str] = field(default_factory=list)  # keywords already in the profile
    ignore_words: set[str] = field(default_factory=set)    # query + location words
    leads: dict[str, set[str]] = field(default_factory=dict)

    def add(self, key: str, texts: list[str]) -> None:
        text = " . ".join(t for t in texts if t)[:MAX_CHARS_PER_LEAD]
        if text:
            self.leads[key] = _ngrams(text, MAX_NGRAMS_PER_LEAD)

    def suggest(self, positives: list[str], limit: int = 15) -> list[dict]:
        pos = [self.leads[k] for k in positives if k in self.leads]
        neg = [v for k, v in self.leads.items() if k not in set(positives)]
        if len(pos) < 3:
            return []
        df_pos: Counter = Counter()
        for grams in pos:
            df_pos.update(grams)
        df_neg: Counter = Counter()
        for grams in neg:
            df_neg.update(grams)

        known = [compile_term(t) for t in self.known_terms]
        min_df = max(2, round(0.35 * len(pos)))
        scored = []
        for gram, n in df_pos.items():
            if n < min_df:
                continue
            words = gram.split()
            # Skip phrases about the search itself (query/location words) or made only of filler.
            if any(w in self.ignore_words for w in words):
                continue
            if all(w in STOPWORDS or w in GENERIC for w in words):
                continue
            if any(rx.search(gram) for rx in known):
                continue
            p = n / len(pos)
            q = df_neg[gram] / len(neg) if neg else 0.0
            lift = p - q
            # Single words need a bigger gap to count: they're noisier than phrases.
            if len(words) == 1 and (lift < 0.45 or len(gram) < 6):
                continue
            if len(words) > 1 and lift < 0.2:
                continue
            # Favour multi-word phrases: they're more specific signals.
            scored.append((lift + 0.08 * (len(words) - 1), gram, p, q))
        scored.sort(reverse=True)

        picked: list[tuple] = []
        for item in scored:
            gram = item[1]
            # Skip a phrase if a longer/shorter overlapping phrase already covers it.
            if any(gram in other[1] or other[1] in gram for other in picked):
                continue
            picked.append(item)
            if len(picked) >= limit:
                break
        return [
            {"term": g, "best_leads_pct": round(100 * p), "other_leads_pct": round(100 * q)}
            for _s, g, p, q in picked
        ]


def ignore_words_for(queries: list[str], locations: list[str]) -> set[str]:
    words: set[str] = set()
    for text in [*queries, *locations]:
        words.update(_WORD.findall(text.lower()))
    return words
