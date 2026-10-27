# Prospector

Find prospects on Google Maps, then qualify them by what their **own websites** say. It works like
PhantomBuster's *Google Maps Search Export* + *website keyword* flow, but runs on your own computer
with no subscription.

```
 Google Maps search  ─►  open each listing  ─►  visit website (home, contact, about, services…)
 "dentist in Austin"     name, phone, address,   pull emails + page text
                         website, rating, reviews
                                                ─►  score against your Ideal Customer Profile
                                                    (keywords, required/excluded words, filters)
                                                ─►  leads table, CSV / Excel export
```

**What you get for each business:** name, category, full address, phone, website, Google rating and review
count, Google Maps link, **emails found on the website**, a **0–100 fit score**, the matched keywords,
and the exact sentences on the site where each keyword appeared, so you can see why it scored that way.

---

## Quick start

You need **Python 3.10 or newer** ([python.org/downloads](https://www.python.org/downloads/)). On Windows,
tick **"Add python.exe to PATH"** in the installer.

| Windows | macOS |
|---|---|
| Double-click **`start.bat`** | Double-click **`start.command`** (first time: right-click → Open) |

The first launch takes a few minutes. It creates a private Python environment in `.venv/`, installs the
dependencies, and downloads a Chromium browser for the Google Maps scraper. After that, the dashboard
opens at **http://localhost:8000**.

<details>
<summary>Manual install (any OS)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
python -m playwright install chromium
prospector serve
```
</details>

---

## Using the dashboard

1. **Search tab:** pick one of your profiles from the dropdown, or fill in a quick search:
   * **What to search on Google Maps:** the same words you'd type into Maps (`dentist`, `roofing contractor`).
   * **Where:** cities, neighbourhoods or ZIP codes, one per line. Every *query × location* pair is searched.
   * **Website keywords:** words that show a business fits you. Add `| 3` to make one count three times as much,
     and use `*` as a wildcard (`implant*` matches implant, implants, implantology).
   * **Must mention at least one** / **Disqualify if site mentions:** hard rules.
   * **Filters & limits:** min rating, review range, wanted/unwanted categories, chains to skip,
     require website/phone/email, how many results, minimum score.

   Click **Start search**. Leads appear on the right as they're checked.
2. **Leads tab:** filter (qualified only, has email, status, min score, text search), sort, click a row
   for full details and keyword evidence, set a status (`new → contacted → replied → won/lost`) and notes,
   then **Export CSV / Excel**. Exports use whatever filters are active.
3. **Runs tab:** history of every search, with *Search again* to reload its settings.
4. **Profiles tab:** your saved Ideal Customer Profiles. *Use*, *Duplicate* or *Delete* them.
5. **Settings tab:** Google Places API key, default source, browser options.

**Repeat searches only show new businesses.** With *Skip businesses found in earlier runs* on (the default),
anything already in your database, matched by Google place ID or website domain, is skipped.
Turn it off to re-check everyone.

---

## Ideal Customer Profiles

Profiles are plain YAML files in [`profiles/`](profiles/). The dashboard edits them, and you can also edit,
copy or share the files directly. Three examples are included:
[dentists offering implants](profiles/dentists-implants.yaml),
[commercial roofers](profiles/commercial-roofers.yaml) and
[local shops selling online](profiles/ecommerce-brands.yaml).

```yaml
name: Commercial roofing contractors
description: Roofers with a commercial division
queries:                       # typed into Google Maps
  - roofing contractor
  - commercial roofing
locations:                     # each query is searched in each location
  - Dallas, TX
  - Fort Worth, TX
keywords:                      # website signals: plain = weight 1, or {term: weight}
  - commercial roof*: 3
  - TPO: 3
  - flat roof*: 2
  - storm damage
required_keywords: []          # at least one must appear (optional)
exclude_keywords:              # disqualify if any appear (optional)
  - residential only
filters:
  min_rating: 4.0
  min_reviews: 10
  max_reviews: 2000            # e.g. skip giant companies
  require_website: true
  require_phone: false
  require_email: false
  exclude_closed: true
  include_categories: []       # partial match on the Google category, e.g. [roofing]
  exclude_categories: [roofing supply store]
  exclude_names: []            # e.g. chains: [Aspen Dental]
max_results_per_search: 60     # per query × location
max_leads: 500                 # cap for the whole run (optional)
pages_per_site: 5              # home + 4 inner pages
min_score: 20                  # 0–100
skip_seen: true
```

### How the score works

`score = (sum of weights of keywords found) ÷ (sum of all keyword weights) × 100`

A keyword counts once whether it appears 1 or 50 times, so a site that mentions five of your signals beats
one that repeats a single word. The Google Maps name and category are searched too, along with the home
page and the most useful inner pages (contact, about, services, products, pricing…).
A lead is **qualified** when it passes every filter, has at least one required keyword (if you set any),
has no excluded keyword, and reaches `min_score`. Unqualified leads are kept with the reason
(e.g. *"rating 3.6 < 4.0"*, *"excluded keyword: franchise"*), so nothing disappears silently.

---

## Where listings come from

| | **Google Maps in a browser** (default, free) | **Google Places API** (official) |
|---|---|---|
| Setup | Nothing | API key with billing enabled |
| Cost | Free | Google bills Text Search per request after the monthly free credit; see [pricing](https://developers.google.com/maps/billing-and-pricing/pricing) |
| Speed | ~1 s per listing | Fast |
| Results per search | As many as Maps will scroll to (often 100+) | Max 60 per query |
| Reliability | Can break if Google changes its page, or show a CAPTCHA after heavy use | Stable |
| Review counts | Sometimes hidden in Google's "limited view" for signed-out visitors | Always |
| Terms | Automated access to Google Maps goes against Google's Terms of Service | Fully allowed |

**Auto** (the default) uses the API when a key is saved, and the browser otherwise. You can switch per
search under *How to search*.

**Get an API key:** [Google Cloud Console](https://console.cloud.google.com/) → create a project → enable
billing → *APIs & Services → Library* → enable **Places API (New)** → *Credentials → Create API key*.
Paste it in **Settings** (or put `GOOGLE_PLACES_API_KEY=…` in a `.env` file; see [`.env.example`](.env.example)).

**Need more than 60 results with the API?** Split the area. Search neighbourhoods or ZIP codes
(`78701`, `78702`, …) as separate locations. Duplicates across searches are merged automatically.

**Seeing review counts in browser mode (optional):** run `prospector browser-login`, sign in to a Google
account in the window that opens, then close it. The scraper reuses that browser profile from then on.
Use a spare account: heavy automated use can get an account rate-limited.

---

## Command line

Everything in the dashboard also works from a terminal (activate `.venv` first, or use
`.venv/bin/prospector` / `.venv\Scripts\prospector`):

```bash
prospector profiles                                  # list saved profiles
prospector search -p commercial-roofers --out roofers.xlsx
prospector search -p dentists-implants -l "Denver, CO" -l "Boulder, CO" --limit 200
prospector search -q "med spa" -l "Miami, FL" -k "botox|2" -k "coolsculpting|3" -k "membership" \
                  --min-score 30 --out medspas.csv  # ad-hoc search, no profile needed
prospector runs                                      # history
prospector export --run 3 --with-email --out run3.csv
prospector serve --port 9000                         # dashboard on another port
```

Useful flags for `search`: `--source api|browser`, `--fast` (don't open each listing: quicker, but only a
short address), `--no-website` (skip website visits), `--include-seen`, `--visible` (watch the browser),
`--all` (export unqualified leads too).

---

## Good to know

* **Emails** come from `mailto:` links, page text, Cloudflare-protected addresses, and simple
  obfuscations like `name [at] domain [dot] com`. Addresses on the business's own domain are listed first.
  Placeholder and tracking addresses (`you@example.com`, Sentry/Wix IDs, image filenames) are filtered out.
* **Websites that block bots** (Cloudflare "Just a moment…", CAPTCHA pages) are not bypassed. The lead is
  kept and marked *"website blocks automated visitors, check it manually"*.
* **robots.txt** is respected by default (Settings). Inner pages a site disallows are skipped.
* **JavaScript-heavy sites** (Wix, Squarespace…) that return almost no text are rendered in Chromium
  automatically. Turn this off in Settings if you want faster runs.
* **Cold outreach rules:** follow the laws where you and your prospects are, e.g. CAN-SPAM in the US
  (identify yourself, include a postal address and an opt-out) and GDPR / PECR in the EU and UK, which
  restrict unsolicited email to individuals.
* Your data stays on your computer: `data/prospector.db` (SQLite) and `data/settings.json`.

### Troubleshooting

| Problem | Fix |
|---|---|
| "Chromium isn't installed for Playwright" | `python -m playwright install chromium` (or delete `.venv` and run the launcher again) |
| "Google is showing a CAPTCHA" | Wait a while, run with `--visible` / untick *Hide the browser window* and solve it yourself, or use an API key |
| Browser search returns 0 results | Try the search in normal Google Maps; check spelling of the location; Google may have changed its page, so use the API source |
| "Google Places API error 403" | Enable **Places API (New)** for the key's project and make sure billing is on |
| Port 8000 already in use | `prospector serve --port 8010` |

---

## Project layout

```
prospector/
  sources/maps_browser.py   Google Maps scraping with Playwright (scroll results, open listings)
  sources/places_api.py     Google Places API (New) Text Search
  enrich/website.py         website crawler (page selection, robots.txt, JS rendering, bot-wall detection)
  enrich/emails.py          email extraction, de-obfuscation, junk filtering, ranking
  icp.py                    Ideal Customer Profile model + YAML profile store
  scoring.py                filters, keyword matching with evidence snippets, 0–100 score
  pipeline.py               orchestration: search → dedupe → crawl in parallel → score → save
  storage.py                SQLite runs + leads (status, notes, "seen before" lookups)
  export.py                 CSV / Excel export
  web/                      FastAPI backend + single-page dashboard (no build step)
  cli.py                    command-line interface
profiles/                   your ICPs (YAML)
tests/                      offline test suite (no network needed)
```

Run the tests with `pip install -e ".[dev]"` then `pytest`.
