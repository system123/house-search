# House Search — Automation Notes

This repo is updated by a daily Cursor cron run (`Property Search — Scheduled Run`).
The job pulls in new listings, refreshes statuses, and proposes scores. This
file documents the workflow for both the user and the agent.

## How the cron knows about user updates

There is no special pull/merge step. The mechanism is implicit:

1. **Cron always starts from the latest `main`.** Each scheduled run gets a
   fresh worktree off `main` HEAD, so any direct edits the user made to
   `listings.csv` on `main` (or any earlier PR the user merged) are already
   present when the run begins.
2. **The user signals "I've reviewed this" via the `status` column.**
   The cron treats a row as "user-touched" when one of these is true:
   - `status` ∈ {`interested`, `rejected`, `viewed`, `contacted`,
     `archived`, `shortlist`, `shortlisted`, `favourite`, `favorite`,
     `offer`}
   - `user_updated_at` is non-empty (the user can write a timestamp here
     if they want to flag a row without changing `status`).
3. **User-touched rows are read-only for non-system fields.** The cron
   never re-scores them, never modifies `notes`, `status`, `score`, or
   `score_reason`. It may still update `listing_status` (e.g. mark sold)
   and `last_checked`.

The detection is implemented in `_sync_user_edits.py`. It also ensures the
`user_updated_at` column exists.

## What the cron promises NOT to do

- It never adds a listing that is already marked `sold` on the source page.
  Sold listings are skipped at scrape time.
- It never overwrites a row's `status` once it is not `unseen`.
- It never deletes rows (use the `archived` status to hide a row).
- It never modifies the budget, bedroom-minimum, regions, or sources at
  runtime; those come from `search-config.json`.

## What the user can edit

Edit `listings.csv` directly on `main`. Recommended workflow:

- After looking at a listing, set `status` to `interested`, `rejected`, or
  `viewed`. You can add free-form notes in the `notes` column.
- If you only want to flag "I've looked at this" without judgement, set
  `user_updated_at` to today's date.
- The user-defined preference signal feeds back into the next run's
  scoring (Step 4 of the cron script).

## Source coverage

The cron currently extracts new listings from these sites:

- **MyProperty** (`www.myproperty.co.za`) — aggregates listings from
  several agents. Uses Playwright + `playwright-stealth` to bypass the
  Vercel bot-protection ("Security Checkpoint") that returns HTTP 429 to
  plain HTTP clients. Server-rendered cards (`data-mp-result-card`)
  contain price, beds/baths, suburb, address slug, and status banner
  (On Show / Under Offer / Sold). Paginated up to 5 pages per region.
- Seeff (`www.seeff.com`)
- Quay1 (`www.quay1.co.za`)
- Greeff (`www.greeff.co.za`)
- Chas Everitt (`www.chaseveritt.co.za`)
- Heads Property (`www.headsproperty.co.za`)
- Jawitz (`www.jawitz.co.za`)

The five white-label sites share a server-rendered `property-card-sm` /
`listing-card` markup that the cron parses for price, bedrooms, and
listing status via plain HTTP.

These sites are listed in `search-config.json` but cannot be scraped from
static HTML alone:

- Property24 (`www.property24.com`) — returns HTTP 503 to scripted clients.
- Engel & Völkers (`www.engelvoelkers.com`) — HTTP 403.
- Cambier Properties (`www.cambierproperties.com`) — HTTP 503.
- Pam Golding (`www.pamgolding.co.za`) — listing cards are JS-rendered;
  the static HTML returns generic featured listings instead of the
  user's filter.
- Sotheby's, RE/MAX, Rawson, Private Property, DG Properties, The Storey,
  Surdo — either reject scripted requests, or need a saved-search URL
  similar to the Quay1 `?advanced_search=<guid>` parameter (see below).

If you want to enable any of these, paste the saved-search URL into
`search-config.json` under a new top-level key `saved_searches`, e.g.

```json
"saved_searches": {
  "quay1.co.za": "https://www.quay1.co.za/results/residential/for-sale/cape-town/oranjezicht/?advanced_search=799bc567-5fe4-5703-aafa-56b26d16ab1a"
}
```

(Wiring this into the scrapers is future work — the cron will read these
URLs and use them as the starting point for that source's region searches.)

## Files in this repo

- `listings.csv` — the canonical data file (single source of truth).
- `search-config.json` — budget, bedrooms_min, regions, sources.
- `index.html` — static viewer that fetches `listings.csv` from
  `raw.githubusercontent.com/.../main/listings.csv`.
- `run-log.jsonl` — newline-delimited JSON, one entry per cron run.
- `AGENTS.md` — this file.
- Helper scripts (used only by the cron, not by the viewer):
  - `_status_check.py` — refreshes `listing_status` for non-sold rows.
  - `_search_new.py` — searches sources and appends new listings.
  - `_score.py` — applies preference scoring to unseen rows.
  - `_sync_user_edits.py` — adds the `user_updated_at` column and lists
    rows that count as user-touched.
