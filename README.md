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
count, Google Maps link, **emails found on the website** (with a check that the domain can receive mail),
other phone numbers on the site, **social profiles** (Facebook, Instagram, LinkedIn, X, YouTube, TikTok, Yelp),
**website signals** (WordPress/Shopify/Wix…, online booking tool, live chat, contact form, ad pixels, no SSL),
a **0–100 fit score**, the matched keywords with the exact sentences where each appeared, and optionally a
one-line **AI note** on why the business fits.

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
   * **How to search:** listing source, **split big cities into areas** (see below), **also search related
     categories** found on your qualified leads, email checks, **AI fit notes**, and **repeat automatically**
     (daily or weekly; each repeat only brings in businesses that are new since last time).

   Click **Start search**. Leads appear on the right as they're checked. When the run finishes, **keyword
   ideas** appear: phrases common on your best leads' websites but rare on the rest. Click one to add it.
2. **Leads tab:** filter (qualified only, has email, status, min score, text search), sort, click a row
   for full details and keyword evidence, set a status (`new → contacted → replied → won/lost`) and notes.
   Tick rows to **change status in bulk** or **export just those**. **Group chains** shows one row per
   website (the best-scoring branch, with a "3 locations" badge). Export as **All columns**, a
   **HubSpot import** file, or a **cold-email tool** file (Instantly, Lemlist, Apollo, Mailchimp: one row per
   deliverable email with a personalization line). Exports use whatever filters are active.
3. **Runs tab:** history of every search, with *Search again* to reload its settings.
4. **Profiles tab:** your saved Ideal Customer Profiles. *Use*, *Duplicate* or *Delete* them.
5. **Settings tab:** Google Places API key, Anthropic API key (AI notes), default source, pacing, browser
   options, and a **Health check** button that tests every part of the pipeline.

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
split_areas: true              # search each location as a grid of smaller areas
max_areas: 9                   # grid cells per location
expand_queries: false          # also search Google categories common among qualified leads
check_emails: true             # check each email's domain can receive mail
ai_notes: false                # one-line Claude note per qualified lead (needs an Anthropic key)
schedule: weekly               # "", daily or weekly (runs while the dashboard is open)
```

### How the score works

Each keyword earns **credit** from 0 to 1 depending on how strongly the business shows it:

| Where it appears | Credit |
|---|---|
| Google Maps name or category, a page title, heading or meta description, or a detected website signal | 0.9 |
| Ordinary page text | 0.7 |
| Only in the navigation menu or footer | 0.4 |
| …plus, for each extra page that mentions it (up to 2) | +0.1 |
| …plus, if it's mentioned 5+ times | +0.1 |

`score = Σ(weight × credit) ÷ Σ(weight) × 100`

So a roofer whose homepage headline says "Commercial Roofing" outscores one whose menu merely links to
"Commercial". In tests on real Boise dentists this cut the number of leads tied at a perfect 100 from 11 to 2.
The lead's detail panel shows each keyword's credit, where it was found, and the sentence it appeared in.

**Synonyms:** separate alternatives with ` / ` (spaces around the slash) to count them as one signal:
`TPO / single-ply / membrane roof* | 3`. A slash without spaces is literal (`24/7`).

**Website signals are searchable:** add `wordpress`, `shopify`, `no ssl` or `booking` as keywords (or
disqualifiers) to target, say, "dentists without online booking".
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

### Covering a whole city: "Split big cities into areas"

One Google Maps search stops at roughly 100–120 results (the API at 60), so a single search for "dentist in
Dallas" misses most of them. With **split into areas** on, Prospector looks each location up on
OpenStreetMap, cuts it into a grid (up to *Max areas*, never smaller than ~3 km), and searches each cell
separately, merging duplicates. In a live test this found **152 dentists in Boise vs 40** from one search.
Listings already found in a neighbouring cell aren't opened again, which keeps it fast.

It's more searches, so it takes longer, and in browser mode Google may show a CAPTCHA after a few hundred
listings. When that happens Prospector pauses for two minutes, slows down, and retries once. If it's still
blocked, the run stops and keeps everything found so far. Raising *Pause between Maps searches* in Settings,
or using the Places API, avoids this.

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
prospector search -p dentists-implants --split-areas --max-areas 16 --expand --out denver.xlsx
prospector export --run 3 --preset hubspot --out hubspot.csv     # or --preset outreach
prospector export --group-chains --with-email --out all.csv
prospector doctor                                    # health check: is everything still working?
prospector serve --port 9000                         # dashboard on another port
```

Useful flags for `search`: `--source api|browser`, `--split-areas`, `--max-areas N`, `--expand`,
`--ai-notes`, `--no-email-check`, `--fast` (don't open each listing: quicker, but only a short address),
`--no-website` (skip website visits), `--include-seen`, `--visible` (watch the browser),
`--all` (export unqualified leads too), `--preset standard|hubspot|outreach`.

**Scheduled runs without the dashboard open:** use your OS scheduler to call the CLI, e.g. a weekly cron
entry `0 7 * * 1 cd /path/to/Webscraper2.0 && .venv/bin/prospector search -p my-profile --out weekly.xlsx`
(on Windows, Task Scheduler running `.venv\Scripts\prospector.exe` with the same arguments). Repeat runs
skip businesses already found.

---

## Good to know

* **Emails** come from `mailto:` links, page text, schema.org structured data, Cloudflare-protected
  addresses, and simple obfuscations like `name [at] domain [dot] com`. If a site's links don't lead to a
  contact page, `/contact`, `/contact-us` and `/about` are tried directly. Addresses on the business's own
  domain are listed first; placeholders and tracking IDs are filtered out. Each address's domain is
  checked for mail servers (via DNS-over-HTTPS): undeliverable ones are flagged and moved last, and the
  cold-email export skips them. This confirms the domain accepts mail, not that the specific mailbox exists.
* **Expect roughly 6 in 10 websites to list an email.** In a test on 87 real dentist and roofer sites, 54
  published one; checked by hand, the rest publish none at all. About half of those have a contact form and a
  third take online bookings. Leads show "contact form only" so you know to reach them that way or by phone.
* **AI fit notes** (optional) send each *qualified* lead's Maps details and website text to Claude
  (Anthropic API key in Settings). The default model is `claude-opus-5-5`, about 1–2¢ per lead; set the model
  to `claude-haiku-5-5` in Settings for a fraction of that. If a request is declined by a safety filter, it
  falls back to another model automatically.
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
| Something stopped working | `prospector doctor` (or Settings → Run health check) tells you which part |

---

## Project layout

```
prospector/
  sources/maps_browser.py   Google Maps scraping with Playwright (scroll results, open listings)
  sources/places_api.py     Google Places API (New) Text Search
  enrich/website.py         website crawler (page selection, contact-page guessing, robots.txt, JS rendering, bot walls)
  enrich/page.py            page parsing: text zones, schema.org data, phones, social profiles
  enrich/signals.py         CMS / booking / chat / form / pixel detection
  enrich/emails.py          email extraction, de-obfuscation, junk filtering, ranking
  enrich/mx.py              email domain deliverability check
  geo.py                    geocoding + splitting locations into a search grid
  suggest.py                keyword ideas from your best leads
  ai.py                     optional Claude fit notes
  scheduler.py              daily / weekly re-runs
  doctor.py                 health checks
  icp.py                    Ideal Customer Profile model + YAML profile store
  scoring.py                filters, zone-weighted keyword matching with evidence snippets, 0–100 score
  pipeline.py               orchestration: search → dedupe → crawl in parallel → score → save
  storage.py                SQLite runs + leads (status, notes, "seen before" lookups)
  export.py                 CSV / Excel export
  web/                      FastAPI backend + single-page dashboard (no build step)
  cli.py                    command-line interface
profiles/                   your ICPs (YAML)
tests/                      offline test suite (no network needed)
```

Run the tests with `pip install -e ".[dev]"` then `pytest`.
