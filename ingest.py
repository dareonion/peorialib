#!/usr/bin/env python3
"""Load catalog-scrape JSON into the SQLite store (catalog_db).

This is the path used when scraping happens in a real browser via Claude-in-Chrome
(the live library_lookup.py scraper needs a display). Capture the browser results
as JSON in this shape, then load it:

    uv run ingest.py scraped.json                 # -> peorialib.db
    uv run ingest.py --db other.db scraped.json

Expected JSON:
{
  "checked_at": "2026-07-26T14:00:00",     # ISO8601; when the scrape was taken
  "source": "claude-in-chrome",            # optional
  "profile": "P0_ALL-PPL-NO-EBOOKS",       # optional; search scope
  "records": [
    {
      "record_id": "SD_ILS:1244737",
      "title": "Little blue truck",
      "author": "Schertle, Alice",       # optional
      "year": "2008",                     # optional
      "query": "little blue truck",       # optional; search terms that surfaced it
      "local_count": 1,                    # optional; Peoria-wide available count
      "avail_text": "1 copy ...",          # optional; raw search blurb
      "holdings": [                        # per-branch copies from the detail page
        {"branch": "Peoria PL - Main St", "call_number": "JP SCH",
         "status": "Juvenile Board Book", "material_type": "Book"}
      ]
    }
  ]
}

Records with `holdings` are stored as detail scrapes (per-branch availability).
`local_count`/`avail_text` (with no holdings) are stored as a search snapshot.
"""
from __future__ import annotations

import argparse
import json
import sys

import catalog_db as db


def ingest(path: str, db_path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    checked_at = payload.get("checked_at")
    if not checked_at:
        sys.exit("JSON is missing top-level 'checked_at' (ISO8601 timestamp).")
    source = payload.get("source", "claude-in-chrome")
    profile = payload.get("profile")
    records = payload.get("records", [])

    counts = {"titles": 0, "availability_rows": 0, "search_snapshots": 0}
    conn = db.open_db(db_path)
    with conn:  # single transaction
        for rec in records:
            rid = rec.get("record_id")
            title = rec.get("title") or ""
            if not rid or not title:
                continue
            meta = {k: rec.get(k) for k in
                    ("author", "year", "isbns", "publisher", "phys_desc",
                     "summary", "audience")}
            holdings = rec.get("holdings") or []
            # format guess from the first holding's cues, if any
            if holdings:
                h0 = holdings[0]
                meta["format"] = db.format_guess(
                    h0.get("call_number", ""), h0.get("material_type", ""),
                    h0.get("status", ""))
            db.upsert_title(conn, rid, title, checked_at, meta)
            counts["titles"] += 1

            if holdings:
                sid = db.record_scrape(conn, "detail", checked_at,
                                       query=rec.get("query"), source=source,
                                       profile=profile)
                db.add_availability(conn, sid, rid, title, holdings, checked_at)
                counts["availability_rows"] += len(holdings)
            if rec.get("local_count") is not None or rec.get("avail_text"):
                sid = db.record_scrape(conn, "search", checked_at,
                                       query=rec.get("query"), source=source,
                                       profile=profile)
                call = holdings[0].get("call_number") if holdings else rec.get("call_number")
                db.add_search_snapshot(conn, sid, rid, rec.get("query"), call,
                                       rec.get("local_count"), rec.get("avail_text"),
                                       checked_at)
                counts["search_snapshots"] += 1
    conn.close()
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Load catalog-scrape JSON into SQLite.")
    ap.add_argument("json_file", help="scrape JSON to load")
    ap.add_argument("--db", default="peorialib.db", help="SQLite path (default peorialib.db)")
    args = ap.parse_args(argv)
    counts = ingest(args.json_file, args.db)
    print(f"ingested into {args.db}: "
          f"{counts['titles']} titles, {counts['availability_rows']} availability rows, "
          f"{counts['search_snapshots']} search snapshots")


if __name__ == "__main__":
    main()
