# peorialib

Look up book availability at the **Peoria Public Library** (RSAcat / SirsiDynix
Enterprise) from the command line, plus the curated toddler/preschool pickup
lists that started this repo.

## Contents

**Code** (the source of truth is the SQLite DB; everything else is derived):
- `library_lookup.py` — the availability lookup tool (details below)
- `bayarea_lookup.py` — the same want-list at SCCLD / San José / Mountain View
- `catalog_db.py` — SQLite store: every scrape's per-branch status over time
- `ingest.py` — load browser-captured scrape JSON into the store
- `report.py` — **generates the markdown** from the store (`--write`) + `--matrix`
- `test_library_lookup.py` / `test_catalog_db.py` / `test_report.py` /
  `test_bayarea_lookup.py` — tests

**Generated markdown** — do NOT hand-edit; regenerate with `uv run report.py --write`.
Every scrape (via `library_lookup.py`/`ingest.py`) regenerates them automatically, so
they're always current:
- `books.md` — overview: per-branch on-shelf counts + the full title-×-branch matrix
- `north.md` / `lakeview.md` / `main.md` — per-branch "on the shelf now" shelf-walks
  (with a Chinese / World Language subsection where applicable)
- `bayarea.md` — title × system overview for the Bay Area lookups
- `sccl.md` / `sjpl.md` / `mountainview.md` — per-system, per-branch shelf-walks

## Bay Area lookups

`bayarea_lookup.py` checks the same want-list at three Bay Area systems:

| Key | System | Catalog |
|---|---|---|
| `sccl` | Santa Clara County Library District | BiblioCommons (gateway JSON API) |
| `sjpl` | San José Public Library | BiblioCommons (gateway JSON API) |
| `mvpl` | Mountain View Public Library | classic Innovative WebPAC (HTML) |

No Cloudflare wall on these catalogs, so it's plain HTTP — no browser needed:

```bash
uv run bayarea_lookup.py                          # every DB title, all three systems
uv run bayarea_lookup.py --system sccl --limit 5  # quick spot check
uv run bayarea_lookup.py --resume                 # fill in whatever a crash skipped
uv run bayarea_lookup.py --retry-misses           # also redo titles that never matched
uv run bayarea_lookup.py --title "dear zoo"       # ad-hoc probe; prints, stores nothing
```

Each title is searched as *cleaned title + author surname*, candidates are scored
by normalized title similarity (the pinyin Chinese titles match the catalogs'
romanized fields), and the winning record's per-branch copies land in
`remote_bibs` / `remote_availability`. "That library doesn't hold it" is a valid
result and is recorded too. The Bay Area markdown regenerates after every run.

**`wantlist_*.json`** files hold extra want-list books that have no Peoria
record — added by hand: `wantlist_zh.json` (Traditional-Chinese picks, with the
Traditional-edition ISBN where known; an unmatched title gets one last search by
ISBN) and `wantlist_fr.json` (French picks). Every run merges them into `titles`
(as `WANT:…` rows) before looking anything up. CJK titles are searched in CJK
where the catalog supports it (BiblioCommons) and via pypinyin romanization
where it doesn't (Mountain View's classic WebPAC 502s on CJK); scoring compares
CJK, pinyin, and romanized forms, which also bridges traditional/simplified
variants. Pinyin-vs-pinyin similarity only counts when it's nearly exact —
syllable streams blur together ('zhe shi wo de' would otherwise happily match
*That's Not My Hat*).

## Storing scrapes (SQLite)

Every lookup can be appended to a local SQLite database (`peorialib.db`, gitignored)
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

## Why it needs a real browser

The catalog sits behind a **Cloudflare bot check**. A plain HTTP request (curl,
`requests`, etc.) gets a `403`, and headless automation gets stuck on the
"Just a moment…" interstitial. A *real* browser clears it by running the
challenge JavaScript — so this tool drives one. No CAPTCHA-solving and no
fingerprint spoofing; it just uses an actual browser the way a person would.

## Setup

```bash
uv sync                              # create the venv + install Playwright
uv run playwright install chromium   # one-time browser download (skip if you use --connect)
```

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

## Tests

```bash
uv run python test_library_lookup.py     # or: uv run pytest -q
```

The tests cover the status/availability parsing and run the two page-side
extractors against fixture HTML loaded locally (no network — the live,
Cloudflare-guarded site is never touched).

## Notes

- Availability is a point-in-time snapshot; board books move fast, so treat it as
  "very likely on the shelf," not a reservation.
- The search scope excludes eBooks/eAudio and the federated article index, matching
  the branch shelf-lookup workflow. Adjust `SEARCH_PROFILE` in the script to change it.
