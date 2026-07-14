"""
Parsers for event sources — the collection layer of the pipeline.

Two strategies, run as a fallback chain so a source redesign rarely means silence:

  1. StructuredParser   — reads schema.org JSON-LD / microdata embedded in the page.
                          This is the web standard for event data; it sits in a
                          <script> tag, so a CSS/HTML redesign of the page leaves it
                          intact. First line of defense against layout changes.

  2. LayoutTolerantParser — used when JSON-LD is absent. Extracts event fields from
                          the page's visible TEXT using date/time patterns. Doesn't
                          depend on any DOM structure at all, only on the words being
                          present — so it survives redesigns that kill JSON-LD too.

The chain: JSON-LD → text extraction → both fail → flag as "0 events, investigate."

This module is zero-dependency and self-contained. The demo feeds it mock page
bodies (JSON-LD blobs and plain text) so you can see both strategies resolve.
In production, `fetch_source()` downloads the real HTML and hands the body here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from event_differ import Event


# ============================================================
# Source fetch contract
# ============================================================

@dataclass
class RawPage:
    """What a source fetch returns. url is provenance for every extracted event."""
    source: str
    url: str
    html: str | None = None        # full HTML (production: from requests/Playwright)
    jsonld: list[dict] | None = None   # structured data found on the page
    text: str | None = None        # visible text extracted from HTML


class FetchError(Exception):
    """Raised when a source can't be reached at all (network/down)."""


# ============================================================
# Strategy 1: Structured data (schema.org JSON-LD)
# ============================================================

# schema.org Event → our Event field map. Keys are schema.org property names.
SCHEMA_FIELD_MAP = {
    "name": "name",
    "startDate": "date",      # ISO datetime → date part taken below
    "doorTime": "time",
    "startTime": "time",
    "location": "venue",
    "offers": "price",
    "url": "url",
    "description": "description",
}


def _schema_venue(loc):
    """schema.org location can be a string, a Place object, or a list. Normalize."""
    if isinstance(loc, str):
        return loc or None
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("address", {}).get("streetAddress")
    return None


_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$"}


def _schema_price(offers):
    """offers can be a single Offer, a list, or an aggregate. Pull a human price."""
    if isinstance(offers, dict):
        offers = [offers]
    if isinstance(offers, list) and offers:
        first = offers[0]
        p = first.get("price") or first.get("lowPrice")
        if p in (0, "0", "0.00"):
            return "Free"
        if p is not None:
            cur = first.get("priceCurrency", "USD")
            sym = _CURRENCY_SYMBOLS.get(cur, cur + " ")
            return f"{sym}{p}"
        # No numeric price — fall back to an offer name like "Free" / "RSVP".
        return first.get("name")
    return None


def parse_jsonld(raw: RawPage) -> list[Event]:
    """
    Read schema.org Event nodes from JSON-LD. Because this data lives in a
    <script type="application/ld+json"> tag, a page's CSS/HTML redesign does not
    move or break it — that's why it's the first strategy.
    """
    events: list[Event] = []
    for node in raw.jsonld or []:
        if node.get("@type") != "Event":
            continue
        mapping = {
            "name": node.get("name"),
            "date": (node.get("startDate") or "")[:10] or None,
            "time": _extract_time(node.get("startDate") or node.get("doorTime") or node.get("startTime")),
            "venue": _schema_venue(node.get("location")),
            "price": _schema_price(node.get("offers")),
            "url": node.get("url") or raw.url,
            "source": raw.source,
            "description": node.get("description"),
        }
        if mapping["name"] and mapping["date"]:
            events.append(Event(**mapping))
    return events


# ============================================================
# Strategy 2: Layout-tolerant text extraction
# ============================================================

# Date patterns covering the common natural-language forms on event pages.
DATE_PATTERNS = [
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), None),                                   # 2026-08-15
    (re.compile(r"(\b\d{1,2}/\d{1,2}/\d{4}\b)"), "%m/%d/%Y"),                     # 08/15/2026
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})\b", re.I), "%b %d %Y"),
]

# Require either HH:MM, or a number with am/pm — a bare number is ambiguous with
# a day-of-month and is deliberately NOT matched.
TIME_PATTERN = re.compile(r"(?<!\d)(\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))(?!\d)", re.I)
PRICE_PATTERN = re.compile(r"(\$\s?\d+(?:\.\d{2})?|Free|free|FREE)", )


_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T")      # e.g. 2026-08-15T07:00


def _extract_time(value: str | None) -> str | None:
    """
    Pull a clock time out of a datetime-ish string. ISO datetimes (recognized by
    the YYYY-MM-DD T prefix) are handled specially — take the part after 'T' so
    the day number isn't mistaken for an hour. Plain 'T' anywhere in the string
    (like 'Tickets') is ignored.
    """
    if not value:
        return None
    if _ISO_DATETIME.search(value):           # real ISO datetime → time after 'T'
        time_part = value.split("T", 1)[1]
        m = TIME_PATTERN.search(time_part)
    else:
        m = TIME_PATTERN.search(value)
    return m.group(1).lower().replace(" ", "") if m else None


def _normalize_date(text: str) -> str | None:
    for pat, fmt in DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        if fmt is None:
            return m.group(1)                 # already ISO, single capture group
        # Reconstruct the matched date string from all groups (month+day+year),
        # not a slice — slicing dropped the month and broke strptime.
        raw = " ".join(m.groups())
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_text(raw: RawPage) -> list[Event]:
    """
    Fall back to reading the page's visible text. Walk line by line; when a line
    contains a recognizable date, treat it as an event record and pull name/time/
    price from the surrounding lines. No DOM dependency — survives redesigns that
    strip JSON-LD, as long as the human-readable text still names a date.
    """
    if not raw.text:
        return []
    events: list[Event] = []
    lines = [ln.strip() for ln in raw.text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        date = _normalize_date(ln)
        if not date:
            continue
        # Name heuristic: the non-date line just above (most listings put title above date).
        name = lines[i - 1] if i > 0 else ln
        # Time and price may be on this line or the next.
        scan = " ".join(lines[i:i + 2])
        events.append(Event(
            name=name,
            date=date,
            time=_extract_time(scan),
            venue=None,
            price=(PRICE_PATTERN.search(scan).group(1) if PRICE_PATTERN.search(scan) else None),
            url=raw.url,
            source=raw.source,
            description=None,
        ))
    return events


# ============================================================
# Fallback chain
# ============================================================

@dataclass
class ParseResult:
    events: list[Event]
    strategy: str        # "json-ld" | "text" | "none"
    note: str            # human-readable, surfaces in failure alerts


def parse_page(raw: RawPage) -> ParseResult:
    """
    The chain a real source fetch runs. Returns whatever the strongest strategy
    found, plus which strategy won — so a failure alert can say 'source-x
    switched from json-ld to text this week, may need a look'.
    """
    events = parse_jsonld(raw)
    if events:
        return ParseResult(events, "json-ld", f"{len(events)} events via structured data (layout-resilient)")

    events = parse_text(raw)
    if events:
        return ParseResult(events, "text", f"{len(events)} events via text extraction (JSON-LD absent)")

    return ParseResult([], "none",
                       "0 events — no JSON-LD and no recognizable dates in text (likely layout change)")


# ============================================================
# Demo: mock page bodies showing both strategies
# ============================================================

def demo():
    print("=" * 66)
    print("  PARSER DEMO — same three sources, two strategies + fallback chain")
    print("=" * 66)

    # Source A: has proper schema.org JSON-LD. A redesign of its HTML/CSS leaves
    # the <script> blob untouched, so parsing is unaffected.
    page_a = RawPage(
        source="source-a", url="https://example-a.com/events",
        jsonld=[
            {"@type": "Event", "name": "Morning Yoga", "startDate": "2026-08-15T07:00",
             "location": {"@type": "Place", "name": "Park Studio"},
             "offers": {"price": "10", "priceCurrency": "USD"}},
            {"@type": "Event", "name": "Tech Meetup", "startDate": "2026-08-18T18:30",
             "location": "Park Studio", "offers": {"price": "0", "priceCurrency": "USD", "name": "Free"}},
        ],
    )
    # Source B: no JSON-LD, just plain text listing (the "plain, inconsistent pages"
    # the client copies from by hand today). Text extraction handles it.
    page_b = RawPage(
        source="source-b", url="https://example-b.org/whats-on",
        text="Jazz Night\nAug 16, 2026 9:00pm at Blue Bar\nTickets $20\n\nCraft Workshop\nAug 20, 2026\n$35",
    )
    # Source C: redesign wiped out its JSON-LD AND scrambled the text — 0 events.
    # The chain returns 'none', which surfaces as a named alert instead of silence.
    page_c = RawPage(
        source="source-c", url="https://example-c.net/list",
        jsonld=[], text="Welcome to our new site! Check back soon for updates.",
    )

    for label, page in [("source-a (structured)", page_a),
                        ("source-b (messy text)", page_b),
                        ("source-c (redesigned → broke)", page_c)]:
        res = parse_page(page)
        print(f"\n  {label}")
        print(f"    strategy: {res.strategy} — {res.note}")
        for e in res.events:
            print(f"    • {e.name} | {e.date} {e.time or ''} | {e.venue or '?'} | {e.price or '?'}")


if __name__ == "__main__":
    demo()
