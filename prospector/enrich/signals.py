"""Detect what a website is built with and what it offers: CMS, online booking, live chat,
contact forms, analytics/ad pixels, SSL. Handy for selling web, marketing or software services
("Wix sites with no online booking", "no SSL", "runs Google Ads")."""

from __future__ import annotations

import re

# (label, pattern matched against the raw HTML, lower-cased)
CMS = [
    ("WordPress", r"wp-content/|wp-includes/|<meta name=\"generator\" content=\"wordpress"),
    ("WooCommerce", r"woocommerce"),
    ("Shopify", r"cdn\.shopify\.com|myshopify\.com|shopify\.theme"),
    ("Wix", r"static\.wixstatic\.com|wix-warmup-data|x-wix-"),
    ("Squarespace", r"static1\.squarespace\.com|squarespace-cdn|<!-- this is squarespace"),
    ("Webflow", r"webflow\.(?:js|com/css|io)|data-wf-site"),
    ("GoDaddy Website Builder", r"img1\.wsimg\.com|godaddysites\.com"),
    ("Weebly", r"weebly\.com|editmysite\.com"),
    ("Duda", r"dudamobile|multiscreensite|irp\.cdn-website\.com"),
    ("HubSpot CMS", r"hs-sites\.com|hubspot\.net/hub/|hs-scripts\.com"),
    ("Joomla", r"/media/jui/|<meta name=\"generator\" content=\"joomla"),
    ("Drupal", r"drupal-settings-json|/sites/default/files/|drupal\.js"),
    ("BigCommerce", r"cdn\d*\.bigcommerce\.com"),
    ("Showit", r"showit\.co|showitcdn"),
]

BOOKING = [
    ("Calendly", r"calendly\.com"),
    ("Acuity", r"acuityscheduling\.com"),
    ("Zocdoc", r"zocdoc\.com"),
    ("Booksy", r"booksy\.com"),
    ("Vagaro", r"vagaro\.com"),
    ("Mindbody", r"mindbodyonline\.com|healcode"),
    ("Square Appointments", r"squareup\.com/appointments|square\.site/book|book\.squareup"),
    ("Setmore", r"setmore\.com"),
    ("Schedulicity", r"schedulicity\.com"),
    ("OpenTable", r"opentable\.com"),
    ("Resy", r"resy\.com"),
    ("LocalMed", r"localmed\.com"),
    ("NexHealth", r"nexhealth\.com"),
    ("Housecall Pro", r"housecallpro\.com"),
    ("Jobber", r"getjobber\.com|clienthub\.getjobber"),
    ("ServiceTitan", r"servicetitan\.com|st-scheduler"),
    ("Fresha", r"fresha\.com"),
    ("Jane App", r"janeapp\.com"),
    ("Tock", r"exploretock\.com"),
]

CHAT = [
    ("Intercom", r"widget\.intercom\.io|intercomcdn"),
    ("Drift", r"js\.driftt\.com|drift\.com/"),
    ("Tawk.to", r"embed\.tawk\.to"),
    ("LiveChat", r"cdn\.livechatinc\.com"),
    ("Crisp", r"client\.crisp\.chat"),
    ("Zendesk Chat", r"static\.zdassets\.com|zopim"),
    ("Olark", r"static\.olark\.com"),
    ("Podium", r"podium\.com|podium-webchat"),
    ("Birdeye", r"birdeye\.com"),
    ("Tidio", r"code\.tidio\.co"),
    ("Freshchat", r"wchat\.freshchat\.com"),
    ("HubSpot Chat", r"js\.usemessages\.com"),
    ("Smartsupp", r"smartsuppchat"),
    ("Gorgias", r"gorgias\.chat"),
]

TRACKING = [
    ("Google Analytics", r"googletagmanager\.com/gtag|google-analytics\.com|gtag\('config', *'g-"),
    ("Google Tag Manager", r"googletagmanager\.com/gtm\.js"),
    ("Google Ads", r"googleadservices\.com|gtag\('config', *'aw-"),
    ("Meta Pixel", r"connect\.facebook\.net/[^\"']*/fbevents\.js|fbq\('init'"),
    ("TikTok Pixel", r"analytics\.tiktok\.com"),
    ("LinkedIn Insight", r"snap\.licdn\.com"),
    ("Hotjar", r"static\.hotjar\.com"),
]

_COMPILED = [
    (group, label, re.compile(rx))
    for group, items in (("cms", CMS), ("booking", BOOKING), ("chat", CHAT), ("tracking", TRACKING))
    for label, rx in items
]
_BOOK_TEXT = re.compile(r"\b(book (?:an |your )?(?:appointment|online|now)|schedule (?:an |your )?(?:appointment|online)|request an appointment)\b", re.I)
_CART = re.compile(r"add[ -]to[ -]cart|/cart\b|shopping cart", re.I)


def detect_signals(html: str, has_form: bool = False, visible_text: str = "") -> list[str]:
    """Return human-readable signal labels, e.g. ['WordPress', 'Booking: Calendly', 'Contact form']."""
    low = (html or "").lower()
    found: list[str] = []
    for group, label, rx in _COMPILED:
        if rx.search(low):
            prefix = {"cms": "", "booking": "Booking: ", "chat": "Chat: ", "tracking": ""}[group]
            found.append(prefix + label)
    if not any(s.startswith("Booking: ") for s in found) and _BOOK_TEXT.search(visible_text or ""):
        found.append("Booking: online form")
    if has_form:
        found.append("Contact form")
    if _CART.search(low):
        found.append("Online store")
    return found


def merge_signals(*groups: list[str]) -> list[str]:
    out: list[str] = []
    for g in groups:
        for s in g:
            if s not in out:
                out.append(s)
    # "Booking: online form" is only a fallback when no booking tool was recognised.
    if any(s.startswith("Booking: ") and s != "Booking: online form" for s in out) and "Booking: online form" in out:
        out.remove("Booking: online form")
    return out
