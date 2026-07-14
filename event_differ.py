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
# Source fetching with isolation — one broken site ≠ a dead run
# ============================================================

def fetch_source(spec: SourceSpec, mock_pool: dict[str, list[Event]]) -> list[Event]:
    """
    Each source is fetched independently. A missing/empty result here models a
    real failure (site down, or returned 200 but parser found nothing — the
    silent-layout-change case). The caller catches it and keeps going with the
    other sources.

    In production this swaps in the real parser named in spec.parser; the mock
    stands in for it.
    """
    if spec.name not in mock_pool:
        raise RuntimeError(f"{spec.name}: fetch failed — 0 events parsed (selector drift or site down)")
    return mock_pool[spec.name]


def run_weekly(prev_snapshot: dict[str, Event], mock_pool: dict[str, list[Event]], store: DraftStore):
    """
    The Wednesday run. Pulls every source, builds this week's snapshot, diffs
    against last week, and writes only NEW / UPDATED / CANCELLED into review.
    Unchanged events are skipped — no duplicate drafts.

    Returns (current_snapshot, failed_sources) so next week has a baseline and
    we know which sources to trust for cancellation calls.
    """
    print("\n" + "=" * 66)
    print("  WEEKLY RUN — Wednesday pull")
    print("=" * 66)

    current: dict[str, Event] = {}
    failed: set[str] = set()

    for spec in SOURCES:
        try:
            events = fetch_source(spec, mock_pool)
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
# Mock data: two snapshots that tell a clean story
# ============================================================

def _ev(name, date, source, **kw):
    return Event(name=name, date=date, source=source,
                 url=f"https://{source}.example/{date}", **kw)


# Last week's baseline snapshot (what the previous run captured).
LAST_WEEK = {
    event_id(_ev("Morning Yoga", "2026-08-15", "source-a", time="07:00", venue="Park Studio", price="$10")):
        _ev("Morning Yoga", "2026-08-15", "source-a", time="07:00", venue="Park Studio", price="$10"),
    event_id(_ev("Jazz Night", "2026-08-16", "source-b", time="20:00", venue="Blue Bar", price="$20")):
        _ev("Jazz Night", "2026-08-16", "source-b", time="20:00", venue="Blue Bar", price="$20"),
    event_id(_ev("Farmers Market", "2026-08-17", "source-a", time="09:00", venue="Town Sq", price="Free")):
        _ev("Farmers Market", "2026-08-17", "source-a", time="09:00", venue="Town Sq", price="Free"),
}

# This week's pulls per source.
THIS_WEEK_A = [
    _ev("Morning Yoga", "2026-08-15", "source-a", time="07:00", venue="Park Studio", price="$10"),   # unchanged
    _ev("Tech Meetup", "2026-08-18", "source-a", time="18:30", venue="Hub", price="Free"),           # NEW
    # Farmers Market gone from source-a → genuinely looks cancelled
]
THIS_WEEK_B = [
    # Jazz Night time moved 20:00 → 21:00 (same identity, field changed)
    _ev("Jazz Night", "2026-08-16", "source-b", time="21:00", venue="Blue Bar", price="$20"),
]
THIS_WEEK_C = [
    _ev("Art Walk", "2026-08-19", "source-c", time="17:00", venue="Downtown", price="Free"),         # NEW
]


# ============================================================
# main — two scenarios end to end
# ============================================================

def main():
    print("Event Aggregator Demo — weekly snapshot diff + review-store writer")
    print("Zero dependencies. Real Supabase draft writer included (not exercised here).\n")

    store = MockDraftStore()

    # --- Scenario 1: a normal weekly run ---
    healthy_pool = {"source-a": THIS_WEEK_A, "source-b": THIS_WEEK_B, "source-c": THIS_WEEK_C}
    current, _ = run_weekly(LAST_WEEK, healthy_pool, store)

    # --- Scenario 2: source-c goes down (simulated layout change) ---
    # source-c's event (Art Walk) must NOT be flagged as cancelled — we simply
    # didn't see it. This is the "no false alarms when a site has a bad day" case.
    print("\n" + "=" * 66)
    print("  SCENARIO 2 — source-c breaks (simulated layout change)")
    print("=" * 66)
    broken_pool = {"source-a": THIS_WEEK_A, "source-b": THIS_WEEK_B}  # source-c omitted → fetch raises
    run_weekly(current, broken_pool, store)

    # --- Summary of what landed in review ---
    print("\n" + "=" * 66)
    print("  REVIEW QUEUE (drafts that would appear in Lovable)")
    print("=" * 66)
    for row in store.rows:
        print(f"  [{row['status']:<22}] {row.get('name', '?')}")
    print("\n  In production: these are status=draft rows in your Lovable events table.")
    print("  You edit/approve/reject each, then publish. Nothing goes live automatically.")


if __name__ == "__main__":
    main()
