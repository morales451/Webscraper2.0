"""Export leads to CSV or Excel, in three layouts:

  * ``standard`` – everything Prospector knows about each lead
  * ``hubspot``  – column names HubSpot's importer maps automatically (company + contact)
  * ``outreach`` – one row per lead with a deliverable email, for cold-email tools
                   (Instantly, Lemlist, Apollo, Mailchimp…); leads without one are skipped
"""

from __future__ import annotations

import csv
import io
import re

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

PRESETS = ("standard", "hubspot", "outreach")

STANDARD = [
    ("Name", "name"),
    ("Score", "score"),
    ("Qualified", "qualified"),
    ("AI fit", "ai_score"),
    ("AI note", "ai_note"),
    ("Category", "category"),
    ("Best email", "best_email"),
    ("Email check", "best_email_check"),
    ("All emails", "all_emails"),
    ("Phone", "phone"),
    ("Other phones", "other_phones"),
    ("Website", "website"),
    ("Address", "address"),
    ("City", "city"),
    ("State", "state"),
    ("Postal code", "postal_code"),
    ("Rating", "rating"),
    ("Reviews", "reviews"),
    ("Locations (same website)", "chain_size"),
    ("Matched keywords", "matched_keywords"),
    ("Website signals", "signals"),
    ("Facebook", "facebook"),
    ("Instagram", "instagram"),
    ("LinkedIn", "linkedin"),
    ("X / Twitter", "x"),
    ("YouTube", "youtube"),
    ("TikTok", "tiktok"),
    ("Yelp", "yelp"),
    ("Why not qualified", "reasons"),
    ("Status", "status"),
    ("Notes", "notes"),
    ("Google Maps URL", "maps_url"),
    ("Search", "query"),
    ("Website title", "site_title"),
    ("Website problem", "site_error"),
    ("Latitude", "latitude"),
    ("Longitude", "longitude"),
]

HUBSPOT = [
    ("Company name", "name"),
    ("Company Domain Name", "domain"),
    ("Website URL", "website"),
    ("Email", "best_email"),
    ("Phone Number", "phone"),
    ("Street Address", "street"),
    ("City", "city"),
    ("State/Region", "state"),
    ("Postal Code", "postal_code"),
    ("Industry", "category"),
    ("Lead Status", "hubspot_status"),
    ("Facebook Company Page", "facebook"),
    ("LinkedIn Company Page", "linkedin"),
    ("Description", "description"),
]

OUTREACH = [
    ("email", "best_email"),
    ("company_name", "name"),
    ("website", "website"),
    ("phone", "phone"),
    ("city", "city"),
    ("state", "state"),
    ("category", "category"),
    ("personalization", "personalization"),
    ("score", "score"),
]

LAYOUTS = {"standard": STANDARD, "hubspot": HUBSPOT, "outreach": OUTREACH}

# HubSpot's default Lead Status values.
HUBSPOT_STATUS = {
    "new": "NEW", "contacted": "ATTEMPTED_TO_CONTACT", "replied": "CONNECTED",
    "won": "OPEN_DEAL", "lost": "UNQUALIFIED", "ignored": "UNQUALIFIED",
}

# "123 Main St, Austin, TX 78701, United States" -> street / city / state / postal code
_US_TAIL = re.compile(r"^(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")


def split_address(address: str) -> dict:
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if parts and parts[-1] in ("United States", "USA", "US", "Canada", "United Kingdom", "UK", "Australia"):
        parts = parts[:-1]
    out = {"street": "", "city": "", "state": "", "postal_code": ""}
    if len(parts) >= 2:
        m = _US_TAIL.match(f"{parts[-2]}, {parts[-1]}")
        if m:
            out.update(street=", ".join(parts[:-2]), city=m["city"], state=m["state"], postal_code=m["zip"])
            return out
        out.update(street=", ".join(parts[:-1]), city=parts[-1])
    elif parts:
        out["street"] = parts[0]
    return out


def _last10(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def flatten(lead: dict) -> dict:
    emails = lead.get("emails") or []
    checks = lead.get("email_checks") or {}
    deliverable = [e for e in emails if checks.get(e) != "invalid"]
    best = deliverable[0] if deliverable else ""
    positive = [m for m in lead.get("matches") or [] if not m["term"].startswith("NOT: ")]
    socials = lead.get("socials") or {}
    phone = lead.get("phone") or ""
    row = dict(lead)
    row.update(split_address(lead.get("address") or ""))
    row.update(
        best_email=best,
        best_email_check={"ok": "accepts email", "ok_a": "no MX record", "invalid": "undeliverable",
                          "unknown": "not checked"}.get(checks.get(best, ""), ""),
        all_emails=", ".join(emails),
        other_phones=", ".join(p for p in lead.get("site_phones") or [] if _last10(p) != _last10(phone)),
        matched_keywords=", ".join(f"{m['term']} ({m['count']})" for m in positive),
        signals=", ".join(lead.get("signals") or []),
        reasons="; ".join(lead.get("reasons") or []),
        qualified="yes" if lead.get("qualified") else "no",
        hubspot_status=HUBSPOT_STATUS.get(lead.get("status") or "new", "NEW"),
        description=lead.get("ai_note") or lead.get("site_description") or "",
        personalization=lead.get("ai_note") or ", ".join(m["term"].split(" / ")[0] for m in positive[:3]),
        **{k: socials.get(k, "") for k in ("facebook", "instagram", "linkedin", "x", "youtube", "tiktok", "yelp")},
    )
    return {k: ("" if v is None else v) for k, v in row.items()}


def _rows(leads: list[dict], preset: str) -> tuple[list[str], list[list]]:
    columns = LAYOUTS.get(preset, STANDARD)
    rows = []
    for lead in leads:
        row = flatten(lead)
        if preset == "outreach" and not row["best_email"]:
            continue
        rows.append([row.get(key, "") for _, key in columns])
    return [label for label, _ in columns], rows


def to_csv(leads: list[dict], preset: str = "standard") -> bytes:
    header, rows = _rows(leads, preset)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    # BOM so Excel opens UTF-8 correctly (importers like HubSpot accept it too).
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(leads: list[dict], preset: str = "standard") -> bytes:
    header, rows = _rows(leads, preset)
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(header)
    header_fill = PatternFill("solid", fgColor="1F2937")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for row in rows:
        ws.append(row)
    wide = {"Name": 32, "Company name": 32, "AI note": 50, "Best email": 30, "Email": 30, "email": 30,
            "All emails": 40, "Website": 34, "Address": 40, "Matched keywords": 40, "Website signals": 34,
            "Why not qualified": 34, "Notes": 30, "Search": 28, "personalization": 50, "Description": 50}
    for idx, label in enumerate(header, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = wide.get(label, 14)
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
