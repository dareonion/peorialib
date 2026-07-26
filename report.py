#!/usr/bin/env python3
"""Render the catalog store (catalog_db) as human-readable reports.

    uv run report.py --matrix                 # cross-branch ✓/✗ matrix (markdown)
    uv run report.py --matrix --db other.db

The matrix shows the *latest* known status of each title at each Peoria branch:
  ✓ on shelf   · in-library only   ✗ out / unavailable   (blank = not held there)
"""
from __future__ import annotations

import argparse

import catalog_db as db

# Column order for the matrix (closest-to-Ivyleaf first-ish; matches the .md files).
BRANCH_ORDER = ["North", "Lakeview", "Main St", "Lincoln", "McClure", "Outreach"]
CELL = {"available": "✓", "reference": "·", "out": "✗"}


def matrix(db_path: str) -> str:
    conn = db.open_db(db_path)
    rows = db.latest_availability(conn, is_peoria_only=True)

    # title -> {branch -> best state}, keeping the most "available" if copies differ
    rank = {"available": 3, "reference": 2, "out": 1}
    grid: dict[str, dict[str, str]] = {}
    meta: dict[str, tuple] = {}
    for r in rows:
        key = r["record_id"]
        meta.setdefault(key, (r["title"], r["call_number"]))
        cur = grid.setdefault(key, {}).get(r["branch"])
        if cur is None or rank[r["state"]] > rank[cur]:
            grid[key][r["branch"]] = r["state"]
    conn.close()

    header = "| Title | Call # | " + " | ".join(BRANCH_ORDER) + " |"
    sep = "|" + "---|" * (2 + len(BRANCH_ORDER))
    lines = [header, sep]
    for key in sorted(meta, key=lambda k: (meta[k][0] or "").lower()):
        title, call = meta[key]
        cells = [CELL.get(grid[key].get(b, ""), "") for b in BRANCH_ORDER]
        lines.append(f"| {title} | {call or ''} | " + " | ".join(cells) + " |")

    legend = "\nLegend: ✓ on shelf · in-library only ✗ out/unavailable (blank = not held there)"
    return "\n".join(lines) + "\n" + legend


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reports over the catalog store.")
    ap.add_argument("--matrix", action="store_true", help="cross-branch availability matrix")
    ap.add_argument("--db", default="peorialib.db", help="SQLite path (default peorialib.db)")
    args = ap.parse_args(argv)
    if args.matrix:
        print(matrix(args.db))
    else:
        ap.error("nothing to do; pass --matrix")


if __name__ == "__main__":
    main()
