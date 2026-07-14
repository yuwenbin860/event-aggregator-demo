# event-aggregator-demo

A runnable demo of the core mechanism for a **weekly event-listing aggregator with a human review step** — the part that has to be right before anything else matters: no duplicates, real update detection, real cancellation detection, and no false alarms when a source site has a bad week.

```
git clone <repo> && python event_differ.py
```

No dependencies. No database. No API keys. It runs two scenarios end to end and shows exactly what would land in your review queue.

---

## Why this demo exists

A weekly pull from 20+ event sources has three failure modes that look like "the scraper is broken":

1. **Duplicates** — the same event comes back next week and gets added again.
2. **Missed updates** — an event's time moved, but it just re-creates instead of updating.
3. **Phantom cancellations** — a source site is temporarily down, so last week's events vanish, and the system flags them all as "cancelled."

This demo shows how one mechanism — a **stable event identity + weekly snapshot diff + source-health awareness** — solves all three, and how only new/updated/cancelled items land as drafts for review.

---

## The core mechanism

One design decision does most of the work: **event identity is deliberately narrow.**

| Identity includes | Identity excludes | Why |
|---|---|---|
| name + date + venue + source | time, price, description | The excluded fields are exactly the ones that *update*. If `time` were part of identity, a 20:00→21:00 change would look like a brand-new event → duplicate. |

Given that, the weekly diff sorts every event into exactly **one bucket** — no event lands in two, so the review queue can't contain duplicates:

| Bucket | Triggered when | Lands in review as |
|---|---|---|
| **new** | identity not seen last week | `draft` |
| **updated** | identity seen, but time/price/name/venue/description changed | `updated` (with the changed fields listed) |
| **possible_cancellation** | identity seen last week, gone this week, **and its source is healthy** | `possible_cancellation` |
| **indeterminate** | identity seen last week, gone this week, **but its source failed this run** | `indeterminate` (held — no false alarm) |
| **unchanged** | identity + all fields identical | *(skipped — no draft created)* |

The **indeterminate** bucket is the reliability detail. If source-b was down Wednesday, we can't tell "Jazz Night got cancelled" from "we just failed to fetch Jazz Night." So we hold it instead of crying wolf — you only get a cancellation flag when the source is healthy and the event really is gone.

---

## What the demo run shows

`python event_differ.py` runs two scenarios against mock data (real Supabase writer included in code, not exercised):

**Scenario 1 — normal Wednesday run.** Three healthy sources. Result:

```
NEW                2     (Tech Meetup, Art Walk)
UPDATED            1     (Jazz Night — time 20:00 → 21:00)
POSSIBLE CANCEL    1     (Farmers Market — gone from healthy source-a)
UNCHANGED (skip)   1     (Morning Yoga — no draft, no duplicate)
```

**Scenario 2 — source-c breaks** (simulated layout change / site down). Art Walk (from source-c) is **not** flagged as cancelled:

```
FAIL  source-c: fetch failed — 0 events parsed (selector drift or site down)
...
INDETERMINATE      1     (Art Walk — source broke, held not flagged cancel)
ALERT — failed sources this run (would notify you by email):
  • source-c — check if layout changed
```

---

## How this connects to your Lovable site (production)

The demo writes to an in-memory mock. In production, new/updated/cancelled events are written as rows into your Lovable events table (Lovable builds on **Supabase**) with `status = 'draft'` — so they appear in your existing Lovable review flow. You edit, approve, or reject each, then publish yourself. **Nothing publishes automatically.**

The real writer is in the code as `SupabaseDraftStore`:

```python
class SupabaseDraftStore(DraftStore):
    def __init__(self, url, key, table="events"):
        from supabase import create_client
        self.client = create_client(url, key)
        self.table = table

    def upsert_draft(self, event, status, reason):
        row = {
            "identity": event_id(event),     # stable across weeks → upsert key
            "name": event.name, "date": event.date, "time": event.time,
            "venue": event.venue, "price": event.price, "url": event.url,
            "source": event.source, "description": event.description,
            "status": status,                # 'draft' | 'updated'
            "review_reason": reason,
        }
        # on_conflict='identity' → updates overwrite instead of duplicating
        self.client.table(self.table).upsert(row, on_conflict="identity").execute()
```

The `on_conflict='identity'` clause is what turns "this event already exists" into an in-place update rather than a duplicate row — the same identity that powers the diff also powers the upsert.

### Adding/removing a source

One entry in the source registry, no code:

```python
SOURCES = [
    SourceSpec("source-a", "https://example-a.com/events",   "structured", "calendar_parser"),
    SourceSpec("source-b", "https://example-b.org/whats-on", "messy",     "layout_tolerant_parser"),
    SourceSpec("source-c", "https://example-c.net/list",     "structured", "listing_parser"),
]
```

In production this is a `sources.yaml` you edit (or a simple form on top). Each source is isolated — `source-b` breaking never stops `source-a` and `source-c` from being processed.

---

## Design trade-offs (the decisions worth questioning)

- **Identity = name + date + venue + source, not a source-site event ID.** Most source sites don't expose a stable ID, and even when they do, it's not comparable across sites. A content hash of stable fields is portable across all 25+ sources. Trade-off: if a venue renames ("Blue Bar" → "Blue Bar & Grill"), it'll look like a new event. Mitigation: venue normalization in the parser layer before identity is computed.

- **`indeterminate` is a real status, not an error to suppress.** It would be simpler to just flag "gone = cancelled." But on a 25-source weekly pull, at least one source is usually flaky on any given Wednesday — that would mean false cancellation alarms every week, and you'd stop trusting the cancellation flag. Holding indeterminate keeps the cancellation signal honest.

- **Source isolation at the fetch layer, not the review layer.** Each source is fetched in its own try/except. A failure records an alert and contributes nothing this week — but the run completes with the other sources. This is why "one site redesigning" never produces an empty review queue for all 25.

- **Unchanged events are skipped entirely.** The review queue only ever contains things that need your attention. On a stable week with 200 live events where 3 changed, you review 3 items, not 200.

---

## What's intentionally NOT in this demo

- **The actual parsers** (calendar/listing/layout-tolerant extractors) — those are per-source and need your real source list. The demo's `mock_pool` stands in for them; the production fetcher swaps in the parser named in each `SourceSpec`.
- **The Wednesday scheduler** (n8n or cron) — trivial to bolt on; the `run_weekly()` function is the entry point it calls.
- **Lovable/Supabase connection details** — the writer is written in full but not exercised, since it needs your real credentials.

These are all covered in Milestone 1 of the project plan once we have your source list and Lovable backend details.
