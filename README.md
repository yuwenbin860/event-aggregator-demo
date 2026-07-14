# event-aggregator-demo

A runnable demo of a **weekly event-listing aggregator with a human review step** — the full pipeline from collection through review, focused on the parts that have to be right before anything else matters:

- **Collection that survives redesigns** — a parser fallback chain, not one fragile selector
- **No duplicates, real update detection, real cancellation detection** — stable identity + snapshot diff
- **No false alarms when a source has a bad week** — source-health-aware diffing
- **Drafts land for review, never auto-published** — to Lovable (Supabase) and/or a CSV you open in Excel

```
git clone <repo> && python event_differ.py        # full pipeline, zero dependencies
python parsers.py                                  # parser fallback chain in isolation
```

No dependencies. No database. No API keys. It runs realistic scenarios end to end.

---

## The pipeline at a glance

```
sources.json          each source = one entry (add/remove a site = edit this file, no code)
    │
    ▼
fetch_source ──► parsers.parse_page ──►  JSON-LD  ──► (found events) ──┐
                  (fallback chain)       (absent?)     │                ├──► Event objects
                                        ▼ text          │                │    (normalized to your fields)
                                        extraction ─────┘                │
                                        ▼ none → alert                    │
    ◄───────────────────────────────────────────────────────────────────┘
    │
    ▼
diff_snapshots(last_week, this_week)  ──►  new / updated / possible_cancel / indeterminate / unchanged
    │
    ▼
DraftStore  ──►  Lovable/Supabase status=draft  +  review_queue.csv (Excel)
              (you edit / approve / reject / publish — nothing goes live automatically)
```

---

## Core mechanism 1 — collection that survives redesigns

Two strategies run as a fallback chain (`parsers.py`), so a source redesign rarely means silence:

| Strategy | What it reads | Why it survives redesigns |
|---|---|---|
| **JSON-LD** (first) | `schema.org Event` nodes in a `<script>` tag | Structured data lives in a script blob, decoupled from the page's HTML/CSS — a visual redesign leaves it intact |
| **Text extraction** (fallback) | The page's visible text, via date/time patterns | Doesn't depend on ANY DOM structure, only on the words being present — survives redesigns that strip JSON-LD too |
| **None** (both fail) | — | Surfaces as a named alert ("0 events — likely layout change"), not silent gaps |

The chain returns which strategy won, so an alert can say *"source-x switched from json-ld to text this week — may need a look."* Run `python parsers.py` to see all three cases:

```
source-a (structured)    strategy: json-ld — 2 events via structured data (layout-resilient)
source-b (messy text)    strategy: text    — 2 events via text extraction (JSON-LD absent)
source-c (redesigned)    strategy: none    — 0 events (would trigger alert)
```

---

## Core mechanism 2 — no duplicates, real updates, real cancellations

One design decision does most of the work: **event identity is deliberately narrow.**

| Identity includes | Identity excludes | Why |
|---|---|---|
| name + date + venue + source | time, price, description | The excluded fields are exactly the ones that *update*. If `time` were part of identity, a 20:00→21:00 change would look like a brand-new event → duplicate. |

Given that, the weekly diff (`diff_snapshots`) sorts every event into exactly **one bucket** — no event lands in two, so the review queue can't contain duplicates:

| Bucket | Triggered when | Lands in review as |
|---|---|---|
| **new** | identity not seen last week | `draft` |
| **updated** | identity seen, but time/price/name/venue/description changed | `updated` (with changed fields listed) |
| **possible_cancellation** | identity seen last week, gone this week, **and its source is healthy** | `possible_cancellation` |
| **indeterminate** | identity seen last week, gone this week, **but its source failed this run** | `indeterminate` (held — no false alarm) |
| **unchanged** | identity + all fields identical | *(skipped — no draft created)* |

The **indeterminate** bucket is the reliability detail. If source-b was down Wednesday, we can't tell "Jazz Night got cancelled" from "we just failed to fetch Jazz Night." So we hold it instead of crying wolf — you only get a cancellation flag when the source is healthy and the event really is gone.

---

## What the demo run shows

`python event_differ.py` runs the full pipeline through two scenarios:

**Scenario 1 — normal Wednesday run**, all sources healthy. source-a serves JSON-LD, source-b serves plain text (the "inconsistent pages" the client copies from by hand today), source-c serves JSON-LD:

```
OK    source-a (structured): 2 events
OK    source-b (messy):     1 events
OK    source-c (structured): 1 events

NEW                2     (Tech Meetup, Art Walk)
UPDATED            1     (Jazz Night — time 8:00pm → 9:00pm)
POSSIBLE CANCEL    1     (Farmers Market — gone from healthy source-a)
UNCHANGED (skip)   1     (Morning Yoga — no draft, no duplicate)
```

**Scenario 2 — source-c redesigns** (JSON-LD gone, text scrambled → 0 events parsed). Art Walk is **not** flagged as cancelled:

```
FAIL  source-c: 0 events parsed — no JSON-LD and no recognizable dates in text (likely layout change)

INDETERMINATE      1     (Art Walk — source broke, held not flagged cancel)
ALERT — failed sources this run (would notify you by email):
  • source-c — check if layout changed
```

Then it exports the review queue to `review_queue.csv` — open it in Excel:

| status | reason | name | date | time | venue | price | source |
|---|---|---|---|---|---|---|---|
| draft | new listing | Tech Meetup | 2026-08-18 | 18:30 | Hub | Free | source-a |
| updated | changed: time | Jazz Night | 2026-08-16 | 9:00pm | | $20 | source-b |
| possible_cancellation | disappeared from source | Farmers Market | | | | | |

---

## How this connects to your Lovable site (production)

The demo writes to an in-memory mock + CSV. In production, new/updated/cancelled events are written as rows into your Lovable events table (Lovable builds on **Supabase**) with `status = 'draft'` — so they appear in your existing Lovable review flow. You edit, approve, or reject each, then publish yourself. **Nothing publishes automatically.**

The real writer is in the code as `SupabaseDraftStore`:

```python
class SupabaseDraftStore(DraftStore):
    def __init__(self, url, key, table="events"):
        from supabase import create_client
        self.client = create_client(url, key)

    def upsert_draft(self, event, status, reason):
        row = {"identity": event_id(event), "name": ..., "status": status, ...}
        # on_conflict='identity' → updates overwrite instead of duplicating
        self.client.table(self.table).upsert(row, on_conflict="identity").execute()
```

The `on_conflict='identity'` clause is what turns "this event already exists" into an in-place update rather than a duplicate row — the same identity that powers the diff also powers the upsert.

### Adding/removing a source

One entry in `sources.json`, no code:

```json
{
  "sources": [
    {"name": "source-a", "url": "https://example-a.com/events", "kind": "structured"},
    {"name": "source-b", "url": "https://example-b.org/whats-on", "kind": "messy"}
  ]
}
```

Add a new site = add one object. Remove a site = delete its object. Each source is isolated — `source-b` breaking never stops `source-a` and `source-c` from being processed.

---

## Design trade-offs (the decisions worth questioning)

- **Identity = name + date + venue + source, not a source-site event ID.** Most source sites don't expose a stable ID, and even when they do, it's not comparable across sites. A content hash of stable fields is portable across all 25+ sources. Trade-off: if a venue renames ("Blue Bar" → "Blue Bar & Grill"), it'll look like a new event. Mitigation: venue normalization in the parser layer before identity is computed.

- **Parser fallback chain (JSON-LD → text), not one selector per source.** Per-source CSS selectors are fast to write but die on the first redesign — exactly the fragility the client is trying to escape. The chain trades a little per-source precision for a lot of robustness. Trade-off: text extraction is less precise on fields like venue (the demo shows this — source-b's Jazz Night has no venue). Mitigation: for sites where venue matters and JSON-LD is absent, a site-specific parser plugs into the same `RawPage` contract.

- **`indeterminate` is a real status, not an error to suppress.** It would be simpler to just flag "gone = cancelled." But on a 25-source weekly pull, at least one source is usually flaky on any given Wednesday — that would mean false cancellation alarms every week, and you'd stop trusting the cancellation flag. Holding indeterminate keeps the cancellation signal honest.

- **0-events-from-a-healthy-fetch is a loud failure, not silent success.** A source returning an empty result is treated as a break (alert raised), not as "nothing changed this week." This is what catches the silent-layout-change case before it becomes a week of missed events.

- **Unchanged events are skipped entirely.** The review queue only ever contains things that need your attention. On a stable week with 200 live events where 3 changed, you review 3 items, not 200.

---

## What's intentionally NOT in this demo

- **Real HTTP fetching** — `fetch_source` consumes mock `RawPage` bodies so the demo runs with zero deps. The production fetcher swaps in `requests`/Playwright and hands the same `RawPage` to `parse_page`.
- **The Wednesday scheduler** (n8n or cron) — trivial to bolt on; `run_weekly()` is the entry point it calls.
- **Lovable/Supabase connection details** — the writer is written in full but not exercised, since it needs your real credentials.

These are all covered in Milestone 1 of the project plan once we have your source list and Lovable backend details.

---

## Files

| File | Role |
|---|---|
| `event_differ.py` | Pipeline: identity, snapshot diff, review store (mock + real Supabase), weekly run, CSV export |
| `parsers.py` | Collection: JSON-LD parser, text-extraction parser, fallback chain |
| `sources.json` | Source registry — edit this to add/remove sites (no code) |
