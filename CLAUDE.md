# shelfwalk — working notes

Tracks a curated toddler want-list against Bay Area library shelves. Formerly
`peorialib`; **Peoria is retired** (frozen snapshot only — don't propose
re-scrapes or browser work there). The live path is `bayarea_lookup.py` →
SQLite → `report.py`.

## Ground rules

- `uv` for everything: `uv run bayarea_lookup.py`, `uv run pytest -q`,
  `uv add <pkg>`. Never `pip`.
- **The `.md` files are generated artifacts.** Never hand-edit them; change
  `report.py` and run `uv run report.py --write`.
- `shelfwalk.db` is the source of truth and is gitignored. The schema in
  `catalog_db.py` recreates it; `open_db()` migrates missing columns.
- Commit/push only when asked.

## Where things live

| Concern | Place |
|---|---|
| Search, matching, availability, enrichment | `bayarea_lookup.py` |
| Schema + all SQL | `catalog_db.py` |
| Every markdown renderer | `report.py` |
| Want-list data | `wantlist_{en,zh,fr}.json`, `wantlist_exclude.json` |
| Favorite branches | `report.py:FAVORITES` |

## Matching invariants (each one is a bug that already bit)

Every rule below has a test in `test_bayarea_lookup.py`. If a change makes one
fail, the rule is probably right and the change is wrong.

- A candidate's **subtitle is part of its identity** — the bare title never
  scores alone (BiblioCommons files series volumes as `Grumpy Monkey` +
  subtitle `Too Many Bugs`).
- Only **descriptive** subtitles may be dropped for stem matching ("a
  lift-the-flap book"), never volume names. Parallel titles (`= Tren de carga`)
  are exempt.
- Extra same-work editions need ≥ `EDITION_MIN_RATIO` (0.95); short-suffix
  spinoffs live in 0.90–0.95 ("…Caterpillar's Eid", "Dragons Love Tacos 2").
- Editions must share the primary's language; foreign records go through the
  **translation** route, which requires the record's own stated original
  (`Translation of:` note / uniform title) to name the want.
- Pinyin/CJK comparisons need ≥ `PINYIN_MIN_RATIO` (0.85) — syllable streams
  blur ('zhe shi wo de' vs *That's Not My Hat*).
- Movies and music are never candidates; digital editions are tracked but
  carry no shelf state.
- WebPAC queries: fold diacritics, join apostrophes (`can't`→`cant`), drop a
  mid-query `not` (it's a boolean operator), CJK → pinyin (the server 502s).

## Data safety nets

- Every HTTP response is mirrored into `raw_pages`. **Before re-scraping to
  debug a parser, check the mirror** — `db.get_raw_page(conn, url)`.
- Re-lookups supersede wholesale: `replace_remote_editions` +
  `latest_remote_availability` (newest scrape per (system, record) joined to
  currently-matched bibs), so corrected matches leave no stale footprint.
- Systems run in parallel threads, one connection each (WAL); `_pace()` spaces
  requests per host — SCCL and SJPL share the BiblioCommons gateway and it
  403s uncoordinated threads.
