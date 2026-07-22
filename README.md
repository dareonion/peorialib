# peorialib

Look up book availability at the **Peoria Public Library** (RSAcat / SirsiDynix
Enterprise) from the command line, plus the curated toddler/preschool pickup
lists that started this repo.

## Contents

- `library_lookup.py` — the availability lookup tool (details below)
- `test_library_lookup.py` — tests for the parsing + a live-DOM check of the extractors
- `books.md` — curated, branch-verified booklist for a 2–3-year-old (North / Lakeview)
- `north.md` — North Branch shelf-walk (by section and call number)

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
