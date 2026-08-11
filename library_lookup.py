#!/usr/bin/env python3
"""Look up book availability at Peoria Public Library (RSAcat / SirsiDynix Enterprise).

The public catalog sits behind a Cloudflare bot check that rejects non-browser
clients — curl gets a 403, and headless automation is held at the "Just a moment"
interstitial. A *real* browser clears it by running the challenge JavaScript, so
this tool drives one:

  * headed (default)   — a real Chrome/Chromium window; works on any desktop.
  * --connect URL      — attach to a Chrome you already have open (most reliable):
                             chrome --remote-debugging-port=9222
                         then: uv run library_lookup.py --connect http://127.0.0.1:9222 ...
  * --headless         — opt-in; fast, but usually blocked by the bot check.

A persistent profile (./.catalog-profile) keeps the Cloudflare clearance cookie
between runs, so repeat lookups are quick.

First-time setup (see README):
    uv sync
    uv run playwright install chromium     # only needed for launched (non --connect) modes

Examples:
    uv run library_lookup.py "dear zoo"
    uv run library_lookup.py --details "little blue truck"
    uv run library_lookup.py --details --branch north --branch lakeview "moo baa la la la"
    uv run library_lookup.py --available-only --branch north "grumpy monkey"
    uv run library_lookup.py --json "press here" > out.json
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import re
import sys
import urllib.parse
from pathlib import Path

import catalog_db

CATALOG = "https://alsi.sdp.sirsi.net/client/en_US/PeoriaPL"
# Search scope: every Peoria Public Library branch, excluding eBooks/eAudio and
# the federated "online articles" firehose. Matches the branch shelf-lookup flow.
SEARCH_PROFILE = "P0_ALL-PPL-NO-EBOOKS"
PROFILE_DIR = Path(__file__).with_name(".catalog-profile")

# CLI branch name -> the substring the catalog prints in its "Library" column.
BRANCHES = {
    "north": "North",
    "lakeview": "Lakeview",
    "lincoln": "Lincoln",
    "main": "Main St",
    "mcclure": "McClure",
    "outreach": "Outreach",
}

# Detail-page status text meaning the copy is NOT on the shelf to grab right now.
_OUT_MARKERS = ("checked out", "transfer", "transit", "on hold", "in repair",
                "lost", "missing", "damaged", "claimed", "billed", "on order")
# ...meaning it's physically there but can't be borrowed.
_NONCIRC_MARKERS = ("non-circulating", "workroom", "reference", "staff", "display")

# --- Page-side extraction (runs in the catalog's own DOM, same as the by-hand flow) ---

SEARCH_JS = r"""
() => {
  const ws = s => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('.results_cell')].map(c => {
    const a = c.querySelector('.results_bio a');
    const txt = c.innerText || '';
    const id = (c.innerHTML.match(/SD_ILS:\d+/) || [null])[0];
    const call = (txt.match(/Call Number:\s*([^\n]+)/) || [, ''])[1];
    const year = (txt.match(/Publication Date:?\s*(\d{4})/) || [, ''])[1];
    const author = (txt.match(/Author\s+([^\n]+)/) || [, ''])[1];
    const av = c.querySelector('.availableNumber');
    return {
      id,
      title: ws(a ? a.textContent : ''),
      author: ws(author),
      call: ws(call),
      year: year,
      avail: ws(av ? av.textContent : ''),
    };
  }).filter(r => r.title);
}
"""

DETAIL_JS = r"""
() => {
  const ws = s => (s || '').replace(/\s+/g, ' ').trim();
  // The mobile + desktop layers render each cell's text twice; collapse it.
  const dedupe = s => {
    s = ws(s);
    const h = s.length / 2;
    if (s.length && s.length % 2 === 0 && s.slice(0, h) === s.slice(h)) s = s.slice(0, h).trim();
    return s;
  };
  const out = [];
  for (const tr of document.querySelectorAll('table tr')) {
    const cells = [...tr.children].map(td => dedupe(td.innerText)).filter(x => x);
    if (!cells.length) continue;
    const lib = cells[0];
    if (!/^Peoria PL/i.test(lib)) continue;
    let status = (cells[cells.length - 1] || '').replace(/Unknown$/, '').trim();
    out.push({
      branch: lib.replace(/^Peoria PL\s*-\s*/i, ''),
      call: cells[1] || '',
      status: status,
    });
  }
  return out;
}
"""


# --------------------------- pure helpers (unit-tested) ---------------------------

def classify(status: str) -> str:
    """Bucket a copy's status into available / out / reference."""
    s = status.lower()
    if any(m in s for m in _OUT_MARKERS):
        return "out"
    if any(m in s for m in _NONCIRC_MARKERS):
        return "reference"
    return "available"


def status_label(status: str) -> str:
    state = classify(status)
    if state == "available":
        return "✓ on shelf"
    if state == "reference":
        return "· in-library use only"
    s = status.lower()
    if "transfer" in s or "transit" in s:
        return "→ in transit"
    return "✗ checked out"


def summarize_avail(text: str):
    """(count, human_text) from the results-page availability blurb.

    count is an int when the catalog gives a number, 0 when nothing is local,
    or None when it couldn't be parsed.
    """
    text = (text or "").strip()
    if re.fullmatch(r"\d+", text):
        n = int(text)
        return n, f"{n} cop{'y' if n == 1 else 'ies'} available now (Peoria PL)"
    if "No copies available at Peoria PL" in text:
        m = re.search(r"(\d+)\s+cop\w+ available at other", text)
        other = m.group(1) if m else "some"
        return 0, f"none in Peoria PL ({other} at other RSA libraries)"
    if text:
        return None, text
    return None, "availability unknown"


def detail_url(sd_id: str) -> str:
    # Slashes in the entry id are catalog-encoded as $002f (verified by hand).
    return f"{CATALOG}/search/detailnonmodal/ent:$002f$002fSD_ILS$002f0$002f{sd_id}/one"


def filter_branches(rows, branches):
    if not branches:
        return rows
    keep = tuple(BRANCHES[b] for b in branches)
    return [r for r in rows if any(k in r["branch"] for k in keep)]


# --------------------------- browser plumbing ---------------------------

@contextlib.contextmanager
def open_page(connect: str | None, headless: bool):
    """Yield a Playwright page backed by a real browser (or a connected one)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run:  uv sync")

    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    with sync_playwright() as p:
        if connect:
            browser = p.chromium.connect_over_cdp(connect)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                yield page
            finally:
                browser.close()
            return

        launch = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            user_agent=ua,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = p.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception:
            try:
                ctx = p.chromium.launch_persistent_context(**launch)  # bundled chromium
            except Exception as e:  # noqa: BLE001
                sys.exit(
                    f"Could not launch a browser ({e}).\n"
                    "Install one once with:  uv run playwright install chromium\n"
                    "Or attach to your own Chrome:  --connect http://127.0.0.1:9222"
                )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()


def lookup(page, query: str, branches, details: bool, max_results: int, db_ctx=None):
    from playwright.sync_api import TimeoutError as PWTimeout

    url = f"{CATALOG}/search/results?qu={urllib.parse.quote_plus(query)}&lm={SEARCH_PROFILE}"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # Availability numbers arrive via a follow-up AJAX call; waiting for them also
    # rides out the Cloudflare interstitial (elements don't exist until it clears).
    try:
        page.wait_for_function(
            "() => { const n = [...document.querySelectorAll('.availableNumber')];"
            "        return n.length && n.some(x => x.textContent.trim()); }",
            timeout=30000,
        )
    except PWTimeout:
        pass  # zero hits, or hits with no availability widget
    page.wait_for_timeout(500)

    hits = [h for h in page.evaluate(SEARCH_JS) if h.get("id")][:max_results]
    for h in hits:
        n, blurb = summarize_avail(h["avail"])
        h["local_count"] = n
        h["avail_text"] = blurb
        all_rows = fetch_detail(page, h["id"]) if details else []
        h["copies"] = filter_branches(all_rows, branches)  # branch-filtered for display
        if db_ctx is not None:
            persist_hit(db_ctx, query, h, all_rows)
    return hits


def persist_hit(db_ctx, query, hit, all_rows):
    """Write one search hit (+ its full branch holdings) to the SQLite store."""
    conn, checked_at = db_ctx
    rid, title = hit["id"], hit["title"]
    meta = {"author": hit.get("author") or None, "year": hit.get("year") or None}
    if all_rows:
        first = all_rows[0]
        meta["format"] = catalog_db.format_guess(first.get("call", ""), "", first.get("status", ""))
    catalog_db.upsert_title(conn, rid, title, checked_at, meta)
    sid = catalog_db.record_scrape(conn, "search", checked_at, query=query,
                                   source="library_lookup", profile=SEARCH_PROFILE)
    catalog_db.add_search_snapshot(conn, sid, rid, query, hit.get("call"),
                                   hit.get("local_count"), hit.get("avail_text"), checked_at)
    if all_rows:
        sid = catalog_db.record_scrape(conn, "detail", checked_at, query=query,
                                       source="library_lookup", profile=SEARCH_PROFILE)
        holdings = [{"branch": r["branch"], "call_number": r.get("call"),
                     "status": r.get("status"), "material_type": None} for r in all_rows]
        catalog_db.add_availability(conn, sid, rid, title, holdings, checked_at)


def fetch_detail(page, sd_id: str, branches):
    from playwright.sync_api import TimeoutError as PWTimeout

    page.goto(detail_url(sd_id), wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_function(
            "() => { const t = document.querySelector('table');"
            "        return t && t.innerText.length > 50 && !/Searching/i.test(t.innerText); }",
            timeout=20000,
        )
    except PWTimeout:
        pass

    rows = page.evaluate(DETAIL_JS)  # all Peoria branches; caller filters for display
    for r in rows:
        r["state"] = classify(r["status"])
    return rows


# --------------------------- orchestration + output ---------------------------

def run(queries, branches, details, available_only, max_results, as_json, connect,
        headless, db_path=None):
    details = details or bool(branches) or available_only
    db_ctx = None
    conn = None
    if db_path:
        conn = catalog_db.open_db(db_path)
        db_ctx = (conn, datetime.datetime.now().isoformat(timespec="seconds"))

    results = {}
    try:
        with open_page(connect, headless) as page:
            for q in queries:
                hits = lookup(page, q, branches, details, max_results, db_ctx)
                if available_only:
                    if details:
                        for h in hits:
                            h["copies"] = [c for c in h["copies"] if c["state"] == "available"]
                        hits = [h for h in hits if h["copies"]]
                    else:
                        hits = [h for h in hits if (h["local_count"] or 0) > 0]
                results[q] = hits
        if conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()

    if as_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_human(results, details)
    if db_path:
        import report
        report.write_all(db_path)
        print(f"\n(recorded to {db_path}; markdown regenerated)", file=sys.stderr)


def print_human(results, details):
    for query, hits in results.items():
        print(f'\n== "{query}" ==')
        if not hits:
            print("  (no matching copies)")
            continue
        for h in hits:
            meta = ", ".join(x for x in (h["call"], h["year"]) if x)
            head = h["title"] + (f" — {h['author']}" if h["author"] else "")
            print(f"  {head}" + (f"  [{meta}]" if meta else ""))
            if details:
                if not h["copies"]:
                    print("      (no copies at the selected branch)")
                for c in h["copies"]:
                    print(f"      {status_label(c['status']):<22} {c['branch']:<10} {c['call']}")
            else:
                print(f"      {h['avail_text']}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Look up book availability at Peoria Public Library (RSAcat).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "branches: " + ", ".join(BRANCHES) + "\n\n"
            "Without --details you get the fast Peoria-wide copy count.\n"
            "--details (implied by --branch/--available-only) opens each title's\n"
            "holdings page for a branch-by-branch, on-shelf breakdown."
        ),
    )
    ap.add_argument("query", nargs="+", help="book title or keywords (repeatable)")
    ap.add_argument(
        "--branch", action="append", choices=list(BRANCHES), metavar="BRANCH",
        help="limit to a branch (repeatable); implies --details",
    )
    ap.add_argument("--details", action="store_true",
                    help="fetch branch-level, on-shelf status for each title")
    ap.add_argument("--available-only", action="store_true",
                    help="show only copies/titles actually on the shelf now")
    ap.add_argument("--max", type=int, default=8, metavar="N",
                    help="max titles to inspect per query (default 8)")
    ap.add_argument("--connect", metavar="URL",
                    help="attach to a running Chrome's CDP endpoint, e.g. http://127.0.0.1:9222")
    ap.add_argument("--headless", action="store_true",
                    help="run the browser headless (faster; usually blocked by the bot check)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--db", default="shelfwalk.db", metavar="PATH",
                    help="SQLite store to append each scrape to (default shelfwalk.db)")
    ap.add_argument("--no-db", action="store_true", help="don't record this run to SQLite")
    args = ap.parse_args(argv)

    run(
        queries=args.query,
        branches=args.branch or [],
        details=args.details,
        available_only=args.available_only,
        max_results=args.max,
        as_json=args.json,
        connect=args.connect,
        headless=args.headless,
        db_path=None if args.no_db else args.db,
    )


if __name__ == "__main__":
    main()
