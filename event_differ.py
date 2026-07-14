"""
Event Aggregator Demo — weekly snapshot diff for event listings.

Zero-dependency runnable demo of the core mechanism that answers the three
things a weekly event pull has to get right:
  1. Don't create duplicates        → stable event identity across weeks
  2. Detect updates, don't re-create → diff fields against last week's snapshot
  3. Detect cancellations            → event seen last week, gone this week

Plus the reliability layer a non-technical owner needs:
  - Config-driven sources (add/remove a site = one entry, no code)
  - Source isolation: one site breaking never kills the weekly run
  - No false "cancellation" alarms when a source simply failed to fetch

A real Supabase draft-writer (Lovable's backend) is implemented in full below;
the demo runs against an in-memory mock so you can just:
    git clone <repo> && python event_differ.py
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict


# ============================================================
# Event identity & snapshot diff — the core mechanism
# ============================================================

@dataclass
class Event:
    name: str
    date: str            # ISO date, e.g. "2026-08-15"
    time: str | None
    venue: str | None
    price: str | None
    url: str             # source URL — provenance, so you can always verify
    source: str          # which source site, e.g. "source-a"
    description: str | None = None


def _norm(s: str | None) -> str:
    """Collapse whitespace + lowercase so "Jazz Night " and "jazz  night" match."""
    if not s:
        return ""
    return " ".join(s.lower().split())


def event_id(e: Event) -> str:
    """
    Stable identity across weeks: name + date + venue + source.

    Intentionally EXCLUDES time/price/description — those are exactly the fields
    that 'update'. If they were part of identity, a time change would look like a
    brand-new event and you'd get the duplicate listings you're trying to avoid.
    """
    key = "|".join([_norm(e.name), _norm(e.date), _norm(e.venue), _norm(e.source)])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


# Fields that count as a meaningful update vs cosmetic noise.
CHANGE_FIELDS = ("time", "price", "name", "venue", "description")


@dataclass
class DiffResult:
    new: list[Event] = field(default_factory=list)
    updated: list[tuple[Event, list[str]]] = field(default_factory=list)  # (event, changed_fields)
    unchanged: list[Event] = field(default_factory=list)
    possibly_cancelled: list[Event] = field(default_factory=list)   # source healthy, event vanished
    indeterminate: list[Event] = field(default_factory=list)        # source broke — can't tell, hold


def diff_snapshots(
    prev: dict[str, Event],
    curr: dict[str, Event],
    failed_sources: set[str] | None = None,
) -> DiffResult:
    """
    Sorts every event into exactly one bucket — no event lands in two, so the
    review queue never contains duplicates.

    The failed_sources bit is the key reliability detail: if source-b was down
    this week, its last-week events are NOT flagged as cancellations. We can't
    tell 'cancelled' from 'we just failed to fetch it', so we hold them as
    indeterminate rather than crying wolf every time a site has a bad day.
    """
    failed_sources = failed_sources or set()
    out = DiffResult()

    for eid, e in curr.items():
        if eid not in prev:
            out.new.append(e)
        else:
            old = prev[eid]
            changed = [f for f in CHANGE_FIELDS if getattr(old, f) != getattr(e, f)]
            if changed:
                out.updated.append((e, changed))
            else:
                out.unchanged.append(e)

    for eid, e in prev.items():
        if eid in curr:
            continue  # still present, handled above
        if e.source in failed_sources:
            out.indeterminate.append(e)
        else:
            out.possibly_cancelled.append(e)

    return out


# ============================================================
# Source registry — add/remove a site = one entry
# ============================================================

@dataclass
class SourceSpec:
    name: str
    url: str
    kind: str       # "structured" (calendar/listing) | "messy" (plain page)
    parser: str     # which extractor handles this source


# In production this is sources.yaml, edited by the owner — no code touched.
# Here it's data so the demo is self-contained.
SOURCES = [
    SourceSpec("source-a", "https://example-a.com/events",  "structured", "calendar_parser"),
    SourceSpec("source-b", "https://example-b.org/whats-on", "messy",     "layout_tolerant_parser"),
    SourceSpec("source-c", "https://example-c.net/list",     "structured", "listing_parser"),
]


# ============================================================
# Draft store — where review items land (never auto-published)
# ============================================================

class DraftStore:
    """New/updated/cancelled events land here for human review before going live."""
    def upsert_draft(self, event: Event, status: str, reason: str) -> None: ...
    def mark_possible_cancellation(self, event: Event) -> None: ...
    def mark_indeterminate(self, event: Event) -> None: ...


class MockDraftStore(DraftStore):
    """In-memory store so the demo runs with zero deps. Prints what it would write."""
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def upsert_draft(self, event, status, reason):
        self.rows.append({"id": event_id(event), "status": status, "reason": reason, **asdict(event)})
        print(f"   ↳ write status={status:<12} | {event.name} | {reason}")

    def mark_possible_cancellation(self, event):
        self.rows.append({"id": event_id(event), "status": "possible_cancellation",
                          "reason": "disappeared from source", "name": event.name})
        print(f"   ↳ write status=possible_cancellation | {event.name}")

    def mark_indeterminate(self, event):
        self.rows.append({"id": event_id(event), "status": "indeterminate",
                          "reason": "source failed this week — holding", "name": event.name})
        print(f"   ↳ write status=indeterminate    | {event.name} (source broke — not flagged as cancel)")


class SupabaseDraftStore(DraftStore):
    """
    The real writer for production. Lovable builds on Supabase, so we write
    new/updated events as rows with status='draft' into the events table — they
    appear in your existing Lovable review flow as editable drafts. Nothing
    publishes live; you edit/approve/reject, then publish yourself.

    NOT exercised by the demo (needs a real Supabase URL + key), but written in
    full so you can see exactly how drafts land where you already work.
    """
    def __init__(self, url: str, key: str, table: str = "events"):
        from supabase import create_client  # only imported when actually used
        self.client = create_client(url, key)
        self.table = table

    def upsert_draft(self, event, status, reason):
        row = {
            "identity": event_id(event),       # stable across weeks → upsert key
            "name": event.name,
            "date": event.date,
            "time": event.time,
            "venue": event.venue,
            "price": event.price,
            "url": event.url,
            "source": event.source,
            "description": event.description,
            "status": status,                  # 'draft' | 'updated'
            "review_reason": reason,
        }
        # on_conflict='identity' is what makes updates overwrite instead of duplicate
        self.client.table(self.table).upsert(row, on_conflict="identity").execute()

    def mark_possible_cancellation(self, event):
        self.client.table(self.table).update({
            "status": "possible_cancellation",
            "review_reason": "disappeared from source this week",
        }).eq("identity", event_id(event)).execute()

    def mark_indeterminate(self, event):
        self.client.table(self.table).update({
            "status": "indeterminate",
            "review_reason": "source failed this week — not enough info to call it",
        }).eq("identity", event_id(event)).execute()


# ============================================================
# Source config — read from sources.json (owner edits this, no code)
# ============================================================

def load_sources(path: str = "sources.json") -> list[SourceSpec]:
    """Read the source registry. Add/remove a site = add/remove one object there."""
    import json, os
    if not os.path.exists(path):
        return list(SOURCES)       # fall back to built-in defaults
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [SourceSpec(name=s["name"], url=s["url"], kind=s["kind"], parser=s["kind"])
            for s in data.get("sources", [])]


# ============================================================
# Source fetching with isolation — one broken site ≠ a dead run
# ============================================================

def fetch_source(spec: SourceSpec, pages: dict[str, "RawPage"]) -> list[Event]:
    """
    Fetch a source's page and run it through the parser fallback chain
    (JSON-LD → text extraction → none). In production, `pages[spec.name]` is
    replaced by an HTTP/Playwright download; here it's a mock page body.

    Returns parsed events. Raises if the source can't be reached at all OR if
    the parser chain finds 0 events — the second case is the silent-layout-change
    failure that's most dangerous, so we surface it loudly rather than silently
    treating "0 events" as "nothing changed."
    """
    from parsers import parse_page            # local import keeps demo entry simple
    if spec.name not in pages:
        raise RuntimeError(f"{spec.name}: fetch failed — site unreachable")
    result = parse_page(pages[spec.name])
    if not result.events:
        raise RuntimeError(f"{spec.name}: 0 events parsed — {result.note}")
    return result.events


def run_weekly(prev_snapshot: dict[str, Event], pages: dict[str, "RawPage"],
               store: DraftStore, sources: list[SourceSpec] | None = None):
    """
    The Wednesday run. Pulls every source through the parser chain, builds this
    week's snapshot, diffs against last week, and writes only NEW / UPDATED /
    CANCELLED into review. Unchanged events are skipped — no duplicate drafts.

    Returns (current_snapshot, failed_sources) so next week has a baseline and
    we know which sources to trust for cancellation calls.
    """
    print("\n" + "=" * 66)
    print("  WEEKLY RUN — Wednesday pull")
    print("=" * 66)

    current: dict[str, Event] = {}
    failed: set[str] = set()

    for spec in (sources or SOURCES):
        try:
            events = fetch_source(spec, pages)
            for e in events:
                current[event_id(e)] = e
            print(f"  OK    {spec.name} ({spec.kind}): {len(events)} events")
        except Exception as exc:
            failed.add(spec.name)
            # Isolation: record + alert, keep going. This source contributes
            # nothing this week but doesn't kill the whole run.
            print(f"  FAIL  {spec.name} ({spec.kind}): {exc}  → flagged for alert")

    result = diff_snapshots(prev_snapshot, current, failed)

    print("\n  --- diff vs last week ---")
    print(f"  NEW                {len(result.new)}")
    print(f"  UPDATED            {len(result.updated)}")
    print(f"  POSSIBLE CANCEL    {len(result.possibly_cancelled)}")
    print(f"  INDETERMINATE      {len(result.indeterminate)}   (source broke — held, not flagged cancel)")
    print(f"  UNCHANGED (skip)   {len(result.unchanged)}")

    print("\n  --- writing to review store ---")
    for e in result.new:
        store.upsert_draft(e, status="draft", reason="new listing")
    for e, fields in result.updated:
        store.upsert_draft(e, status="updated", reason=f"changed: {', '.join(fields)}")
    for e in result.possibly_cancelled:
        store.mark_possible_cancellation(e)
    for e in result.indeterminate:
        store.mark_indeterminate(e)

    if failed:
        print("\n  ALERT — failed sources this run (would notify you by email):")
        for name in failed:
            print(f"    • {name} — check if layout changed")

    return current, failed


# ============================================================
# CSV export — a concrete, human-readable review queue the owner opens in Excel
# ============================================================

def export_review_csv(store: "MockDraftStore", path: str = "review_queue.csv") -> None:
    """
    Write the review queue to CSV so a non-technical owner can open it in Excel/
    Sheets, eyeball each row, and decide. In production these same rows are also
    status=draft in Lovable; the CSV is a fallback view + an export format for
    any site that can't take direct DB writes.
    """
    import csv
    if not store.rows:
        print("  (review queue empty — nothing to export)")
        return
    fields = ["status", "reason", "name", "date", "time", "venue", "price", "source", "url"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in store.rows:
            w.writerow(row)
    print(f"  → wrote {len(store.rows)} review rows to {path} (open in Excel/Sheets)")


# ============================================================
# Mock page bodies — what fetch_source would download in production
# ============================================================

def _ev(name, date, source, **kw):
    return Event(name=name, date=date, source=source,
                 url=f"https://{source}.example/{date}", **kw)


# Last week's baseline snapshot (what the previous run captured).
# Note: source-b's text extractor can't see venue, so baseline venue is None too
# — this keeps identity consistent week to week so Jazz Night is recognized as an
# UPDATE (time changed) rather than a new event.
LAST_WEEK = {
    event_id(_ev("Morning Yoga", "2026-08-15", "source-a", time="07:00", venue="Park Studio", price="$10")):
        _ev("Morning Yoga", "2026-08-15", "source-a", time="07:00", venue="Park Studio", price="$10"),
    event_id(_ev("Jazz Night", "2026-08-16", "source-b", time="8:00pm", venue=None, price="$20")):
        _ev("Jazz Night", "2026-08-16", "source-b", time="8:00pm", venue=None, price="$20"),
    event_id(_ev("Farmers Market", "2026-08-17", "source-a", time="9:00am", venue="Town Sq", price="Free")):
        _ev("Farmers Market", "2026-08-17", "source-a", time="9:00am", venue="Town Sq", price="Free"),
}


def _make_pages():
    """Mock raw pages for this week's pull. In production these come from HTTP."""
    from parsers import RawPage
    # source-a: structured (schema.org JSON-LD). Layout-resilient.
    page_a = RawPage(
        source="source-a", url="https://example-a.com/events",
        jsonld=[
            {"@type": "Event", "name": "Morning Yoga", "startDate": "2026-08-15T07:00",
             "location": {"@type": "Place", "name": "Park Studio"},
             "offers": {"price": "10", "priceCurrency": "USD"}},                  # unchanged
            {"@type": "Event", "name": "Tech Meetup", "startDate": "2026-08-18T18:30",
             "location": "Hub", "offers": {"price": "0", "priceCurrency": "USD"}}, # NEW
            # Farmers Market gone from source-a → genuinely looks cancelled
        ],
    )
    # source-b: messy plain text (the "plain, inconsistent pages" the client
    # copies from by hand today). No JSON-LD — text extraction handles it.
    page_b = RawPage(
        source="source-b", url="https://example-b.org/whats-on",
        text="Jazz Night\nAug 16, 2026 9:00pm at Blue Bar\nTickets $20",           # time moved → UPDATED
    )
    # source-c: structured, healthy this week.
    page_c = RawPage(
        source="source-c", url="https://example-c.net/list",
        jsonld=[
            {"@type": "Event", "name": "Art Walk", "startDate": "2026-08-19T17:00",
             "location": "Downtown", "offers": {"price": "0", "priceCurrency": "USD"}},  # NEW
        ],
    )
    return {"source-a": page_a, "source-b": page_b, "source-c": page_c}


def _make_broken_pages():
    """Scenario 2: source-c redesigned — JSON-LD gone, text scrambled → 0 events."""
    from parsers import RawPage
    base = _make_pages()
    base["source-c"] = RawPage(
        source="source-c", url="https://example-c.net/list",
        jsonld=[], text="Welcome to our new site! Check back soon for upcoming events.",
    )
    return base


# ============================================================
# main — three scenarios end to end
# ============================================================

def main():
    print("Event Aggregator Demo — collection + diff + review queue")
    print("Zero dependencies. Parser fallback chain (JSON-LD → text) + real Supabase writer.\n")

    sources = load_sources()
    store = MockDraftStore()

    # --- Scenario 1: normal Wednesday run, all sources healthy ---
    current, _ = run_weekly(LAST_WEEK, _make_pages(), store, sources)

    # --- Scenario 2: source-c redesigns and breaks (0 events parsed) ---
    # Its event (Art Walk) must NOT be flagged as cancelled — we can't tell
    # "cancelled" from "we failed to fetch it", so we hold it as indeterminate.
    # This is the "no false alarms when a site has a bad week" case.
    print("\n" + "=" * 66)
    print("  SCENARIO 2 — source-c redesigns (layout change breaks parsing)")
    print("=" * 66)
    run_weekly(current, _make_broken_pages(), store, sources)

    # --- Review queue summary + CSV export ---
    print("\n" + "=" * 66)
    print("  REVIEW QUEUE (what lands for you to review this week)")
    print("=" * 66)
    for row in store.rows:
        print(f"  [{row['status']:<22}] {row.get('name', '?')}")
    print("\n  In production: these are status=draft rows in your Lovable events table,")
    print("  plus a CSV export you can open in Excel. You edit/approve/reject, then publish.")

    print("\n" + "=" * 66)
    print("  CSV EXPORT")
    print("=" * 66)
    export_review_csv(store)


if __name__ == "__main__":
    main()
