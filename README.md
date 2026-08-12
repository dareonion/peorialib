# shelfwalk

Track a curated toddler/preschool want-list against **Bay Area library
systems** — which branches have which books on the shelf right now, across
every edition we can find (board/picture printings, audiobooks, eBooks, and
Chinese / French / Spanish / Japanese translations), with every listing linked
to the live catalog record.

> **History:** this repo began as `peorialib`, a Peoria Public Library
> (RSAcat / SirsiDynix) lookup tool. The Peoria side is no longer refreshed —
> the want-list it seeded lives on in the DB, its markdown remains as a
> snapshot, and the Bay Area lookups below are the live part.

## Contents

**Code** (the source of truth is the SQLite DB; everything else is derived):
- `bayarea_lookup.py` — **the live tool**: the want-list at SCCLD / San José /
  Mountain View / LINK+
- `library_lookup.py` — the retired Peoria scraper (browser-driven; see the end)
- `catalog_db.py` — SQLite store: every scrape's per-branch status over time
- `ingest.py` — load browser-captured scrape JSON into the store
- `report.py` — **generates the markdown** from the store (`--write`) + `--matrix`
- `test_library_lookup.py` / `test_catalog_db.py` / `test_report.py` /
  `test_bayarea_lookup.py` — tests

**Generated markdown** — do NOT hand-edit; regenerate with `uv run report.py --write`.
Every scrape (via `library_lookup.py`/`ingest.py`) regenerates them automatically, so
they're always current:
- `bayarea.md` — **start here**: a *To do* ladder (hold / request through LINK+
  / buy) plus the title × system and your-branches matrices
- `titles.md` — per-title bibliographic detail: ages, ISBN, awards, summary
- `sccl.md` / `sjpl.md` / `mountainview.md` — per-system, per-branch shelf-walks
- `linkplus.md` — LINK+ union catalog, title-centric
- `books.md` + `north.md` / `lakeview.md` / `main.md` — frozen Peoria snapshot

Every listing links straight to the record in that system's own catalog
(BiblioCommons, the classic WebPACs, Peoria's RSAcat), so a click lands on the
live page for holds/renewals.

## Setup

```bash
uv sync    # venv + deps (pypinyin; playwright, only the Peoria scraper needs it)
```

Nothing else — the Bay Area catalogs are plain HTTP, no browser or auth.

## Bay Area lookups

`bayarea_lookup.py` checks the want-list at four Bay Area systems:

| Key | System | Catalog |
|---|---|---|
| `sccl` | Santa Clara County Library District | BiblioCommons (gateway JSON API) |
| `sjpl` | San José Public Library | BiblioCommons (gateway JSON API) |
| `mvpl` | Mountain View Public Library | classic Innovative WebPAC (HTML) |
| `linkplus` | LINK+ union catalog (~70 CA/NV systems) | INN-Reach WebPAC (HTML) |

A LINK+ hit means any member-library copy can be requested for pickup at
Mountain View (searching needs no login; requesting uses your card). Its
`linkplus.md` is title-centric — per-branch shelf-walks make no sense across
seventy library systems.

No Cloudflare wall on these catalogs, so it's plain HTTP — no browser needed.
The four systems run **in parallel** (one thread and one DB connection each;
each host still gets serial, politely-spaced requests — LINK+ rate-limits, so
it paces itself at 1s), which makes a full refresh take as long as the slowest
system instead of the sum of all four:

```bash
uv run bayarea_lookup.py                          # every DB title, all four systems
uv run bayarea_lookup.py --system sccl --limit 5  # quick spot check
uv run bayarea_lookup.py --resume                 # fill in whatever a crash skipped
uv run bayarea_lookup.py --retry-misses           # also redo titles that never matched
uv run bayarea_lookup.py --title "dear zoo"       # ad-hoc probe; prints, stores nothing
uv run bayarea_lookup.py --enrich                 # just the record-detail pass
```

Each title is searched as *cleaned title + author surname*, candidates are scored
by normalized title similarity (the pinyin Chinese titles match the catalogs'
romanized fields), and the winning record's per-branch copies land in
`remote_bibs` / `remote_availability`. "That library doesn't hold it" is a valid
result and is recorded too. The Bay Area markdown regenerates after every run.

**Every version of a matched work is tracked**, not just the best record
(`remote_editions`): other physical formats and printings (board vs picture),
audiobooks (including compilations like *Brown bear & friends* that carry the
story under another title), eBooks/eAudiobooks, and Chinese / French / Spanish
/ Japanese editions. Movies and music are never candidates. Digital editions
are listed and linked but carry no shelf state — a Libby license queue isn't a
shelf — so they get their own section in the per-system markdown. Translations
and audio compilations are only accepted from strict AND-semantics search
results (WebPAC keyword search, BiblioCommons' fielded search) with a matching
author — a translated title can't fuzzy-match the original, but its record
carries the original title, so appearing in those results plus the author is
the anchor. The per-system markdown labels each non-plain version
(`— board book`, `— audiobook: “Brown bear & friends”`, `— Spanish: “Oso
polar, oso polar, ¿qué es ese ruido?”`) — a compilation or translation is its
own work, so its real title always shows, a compilation lists what it contains
(`contains: Brown bear…; Polar bear…`), and a translation names its original
title. Those details come from the record pages (MARC 505/240) in an
enrichment pass that runs automatically after every lookup; `--enrich` runs
just that pass for anything still missing.

**`wantlist_*.json`** files are the hand-curated want-list — one JSON array of
`{title, author, format, lang?, isbn?}`: `wantlist_en.json` (English picks),
`wantlist_zh.json` (Traditional-Chinese, with the Traditional-edition ISBN
where known; an unmatched title gets one last search by ISBN), and
`wantlist_fr.json` (French). `lang` pins a title to that language so a French
want can't match the English edition. `wantlist_exclude.json` lists titles to
leave out of remote lookups entirely (Peoria-only shelf finds). Every run
merges the lists into `titles` (as `WANT:…` rows) before looking anything up,
so adding a book is a one-line edit plus `uv run bayarea_lookup.py --resume`.
CJK titles are searched in CJK
where the catalog supports it (BiblioCommons) and via pypinyin romanization
where it doesn't (Mountain View's classic WebPAC 502s on CJK); scoring compares
CJK, pinyin, and romanized forms, which also bridges traditional/simplified
variants. Pinyin-vs-pinyin similarity only counts when it's nearly exact —
syllable streams blur together ('zhe shi wo de' would otherwise happily match
*That's Not My Hat*).

## Storing scrapes (SQLite)

Every lookup can be appended to a local SQLite database (`shelfwalk.db`, gitignored)
so availability accumulates as a time-series. Two ways in:

```bash
uv run library_lookup.py --details "pigeon needs a bath"   # live scrape, auto-records (--no-db to skip)
uv run ingest.py scrape.json                               # load browser-captured JSON
uv run report.py --matrix                                  # regenerate the cross-branch table
```

Tables: `titles` (record metadata), `scrapes` (one row per lookup event),
`availability` (one row per branch copy per check — the time-series core), and
`search_snapshots` (Peoria-wide counts). The schema lives in `catalog_db.py`, so the
DB is always reproducible; it's kept out of git to avoid binary churn.

**Raw mirror**: every response body the Bay Area lookups fetch — search
results, availability, record details, all four systems — is also stored
verbatim (zlib-compressed) in `raw_pages`, newest fetch per URL. Every field
the source sent stays re-parseable offline, so an extraction or matching fix
can be replayed against mirrored data instead of needing a re-scrape.

**Full details per edition**: the enrichment pass visits every tracked
version's record page (mirror-first — LINK+ pages were already fetched for
holdings) and stores the complete bibliographic picture in
`remote_editions.details` as JSON: ISBN, edition statement, publisher,
physical description, summary, audience, series, subjects, genres, alternate
titles — plus the contents note and stated original title used for
compilation/translation labeling and verification.

## What to do about each title

`bayarea.md` opens with a **To do** section, because "is it on a shelf" isn't
an action. Every tracked title lands in exactly one rung:

1. **on a favorite branch's shelf now** — nothing to do; it's in that system's
   shelf-walk file, call number and all
2. **place a hold** — your systems own it but no favorite branch has a copy
   out on the shelf; the *Speed* column says whether one is sitting on some
   other branch's shelf (fast) or every copy is out (wait)
3. **request through LINK+** — no local system has it, a member library does
4. **buy** — in no catalog here at all, with the ISBN to order

## Per-title detail

`titles.md` carries what the shelf lists have no room for — reading age, ISBN,
awards, and the summary — merged across systems (each field from the first
catalog that recorded it). Ages come from the catalogs' audience notes, which
are wildly inconsistent (`Ages 3-7`, `2-5 Brodart`, `Pre-K to 1`, `AD 280
Lexile`), so the column normalizes whatever exists and blanks where a record
says nothing. The shelf-walk tables carry the same age in a column, for
judging at the shelf whether a book suits the kid you're shopping for.

## Scheduled refresh

Board books turn over within hours, so the reports are only true if they're
fresh. `refresh.sh` re-checks everything, regenerates the markdown, and commits
it (logs in `logs/`, pruned after 30 days; set `SHELFWALK_PUSH=1` to push too).
A systemd user timer runs it at 7:30am, catching up after a suspend:

```bash
cp systemd/shelfwalk-refresh.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shelfwalk-refresh.timer
systemctl --user list-timers shelfwalk-refresh.timer   # when it next fires
journalctl --user -u shelfwalk-refresh -n 20           # what happened
```

(`loginctl enable-linger` if it should also run while you're logged out.)

## Tests

```bash
uv run pytest -q        # 61 tests, no network — every parser runs on fixtures
```

---

# Peoria (retired)

Everything below concerns `library_lookup.py` and the Peoria markdown, kept for
reference. It is no longer refreshed.

## Why it needs a real browser

The catalog sits behind a **Cloudflare bot check**. A plain HTTP request (curl,
`requests`, etc.) gets a `403`, and headless automation gets stuck on the
"Just a moment…" interstitial. A *real* browser clears it by running the
challenge JavaScript — so this tool drives one. No CAPTCHA-solving and no
fingerprint spoofing; it just uses an actual browser the way a person would.
One-time browser download: `uv run playwright install chromium`.

## Usage

```bash
# Fast Peoria-wide copy count:
uv run library_lookup.py "dear zoo"

# Branch-by-branch, on-shelf breakdown (opens each title's holdings page):
uv run library_lookup.py --details "little blue truck"

# Limit to your branches, only what's on the shelf right now:
uv run library_lookup.py --available-only --branch north --branch lakeview "grumpy monkey"

# Several titles at once, as JSON:
uv run library_lookup.py --json "press here" "moo baa la la la" > out.json
```

Branches: `north`, `lakeview`, `lincoln`, `main`, `mcclure`, `outreach`.
`--branch` and `--available-only` both imply `--details`.

### Browser modes

- **headed (default)** — opens a real Chrome/Chromium window. Works on any
  desktop. A persistent profile in `./.catalog-profile` keeps the Cloudflare
  clearance cookie so repeat runs are quick.
- **`--connect URL`** — attach to a Chrome you already have open. Most reliable,
  and needs no `playwright install`:
  ```bash
  google-chrome --remote-debugging-port=9222      # (in another terminal)
  uv run library_lookup.py --connect http://127.0.0.1:9222 --details "dear zoo"
  ```
- **`--headless`** — fastest, but the bot check usually blocks it. Handy only if
  you're running through `--connect` to a headless-but-already-cleared Chrome, or
  on a network Cloudflare trusts.

## Notes

- Availability is a point-in-time snapshot; board books move fast, so treat it as
  "very likely on the shelf," not a reservation.
- The search scope excludes eBooks/eAudio and the federated article index, matching
  the branch shelf-lookup workflow. Adjust `SEARCH_PROFILE` in the script to change it.
