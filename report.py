#!/usr/bin/env python3
"""Render the catalog store (catalog_db) into markdown — the ONLY way the .md files
are produced. They are generated artifacts of `peorialib.db`; never hand-edit them.

    uv run report.py --write      # (re)generate all markdown from the DB
    uv run report.py --matrix     # just print the cross-branch matrix to stdout

`--write` produces:
    books.md      overview: per-branch counts + the full title-x-branch matrix
    north.md      \\
    lakeview.md    }  per-branch "on the shelf now" shelf-walks
    main.md       /

ingest.py and library_lookup.py call `write_all` after every scrape, so the files
stay current automatically.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import catalog_db as db

# All Peoria branches, in the order they appear in the matrix (user's branches first).
BRANCH_ORDER = ["North", "Lakeview", "Main St", "Lincoln", "McClure", "Outreach"]
# Per-branch shelf-walk files we generate (the branches the user actually visits).
SHELF_FILES = {"North": "north.md", "Lakeview": "lakeview.md", "Main St": "main.md"}
CELL = {"available": "✓", "reference": "·", "out": "✗"}
_RANK = {"available": 3, "reference": 2, "out": 1}

BANNER = ("<!-- AUTO-GENERATED from peorialib.db by report.py — do not edit by hand. "
          "Regenerate: `uv run report.py --write` -->")


def _is_world_language(row) -> bool:
    s = (row["status_raw"] or "")
    c = (row["call_number"] or "")
    return "World Language" in s or c.startswith("AUDIO CHINESE") or c.startswith("AUDIO ")


def _shelf_cat(row) -> str:
    """board / picture / other, from the catalog's shelf-location text (status_raw)."""
    s = (row["status_raw"] or "").lower()
    if "board book" in s:
        return "board"
    if "picture book" in s:
        return "picture"
    return "other"


_TYPE_LABEL = {"board": "Board", "picture": "Picture", "other": "Other"}
_TYPE_RANK = {"board": 3, "picture": 2, "other": 1}


def _load(db_path):
    """Return (rows, as_of). rows = latest availability per (record, branch)."""
    conn = db.open_db(db_path)
    rows = db.latest_availability(conn, is_peoria_only=True)
    as_of = conn.execute("SELECT MAX(checked_at) FROM availability").fetchone()[0]
    conn.close()
    return rows, as_of


def _gen_header(as_of):
    return (f"{BANNER}\n\n"
            f"_Auto-generated from `peorialib.db` — data as of **{as_of or 'n/a'}**. "
            f"Don't hand-edit; run `uv run report.py --write`._\n")


def matrix(db_path: str) -> str:
    rows, as_of = _load(db_path)
    return matrix_body(rows) + f"\n\n_As of {as_of or 'n/a'}._\n"


def _books_md(rows, as_of) -> str:
    # per-branch available counts
    avail = {b: set() for b in BRANCH_ORDER}
    for r in rows:
        if r["state"] == "available" and r["branch"] in avail:
            avail[r["branch"]].add(r["record_id"])
    out = ["# Peoria toddler books — overview\n", _gen_header(as_of),
           "\n## On the shelf now (count by branch)\n",
           "| Branch | # on shelf |", "|---|---|"]
    for b in BRANCH_ORDER:
        out.append(f"| {b} | {len(avail[b])} |")
    out.append("\nPer-branch title lists: `north.md`, `lakeview.md`, `main.md`.\n")
    out.append("\n## Full availability matrix\n")
    out.append(matrix_body(rows))
    return "\n".join(out) + "\n"


def matrix_body(rows) -> str:
    grid, meta, types = {}, {}, {}
    for r in rows:
        key = r["record_id"]
        meta.setdefault(key, (r["title"], r["call_number"]))
        cur = grid.setdefault(key, {}).get(r["branch"])
        if cur is None or _RANK[r["state"]] > _RANK[cur]:
            grid[key][r["branch"]] = r["state"]
        cat = _shelf_cat(r)
        if key not in types or _TYPE_RANK[cat] > _TYPE_RANK[types[key]]:
            types[key] = cat
    header = "| Title | Call # | Type | " + " | ".join(BRANCH_ORDER) + " |"
    sep = "|" + "---|" * (3 + len(BRANCH_ORDER))
    lines = [header, sep]
    for key in sorted(meta, key=lambda k: (meta[k][0] or "").lower()):
        title, call = meta[key]
        cells = [CELL.get(grid[key].get(b, ""), "") for b in BRANCH_ORDER]
        lines.append(f"| {title} | {call or ''} | {_TYPE_LABEL[types[key]]} | " + " | ".join(cells) + " |")
    return ("\n".join(lines) + "\nLegend: Type = Board / Picture / Other (readers, holiday, "
            "world-language, or checked out at every branch so the shelf isn't shown); "
            "✓ on shelf · in-library use only ✗ out (blank = not held there)")


def _branch_md(branch, rows, as_of) -> str:
    # Collapse multiple copies of the same title at this branch into one entry,
    # keeping the best state (a title is "on the shelf" if any copy is available).
    best = {}
    for r in (x for x in rows if x["branch"] == branch):
        rid = r["record_id"]
        if rid not in best or _RANK[r["state"]] > _RANK[best[rid]["state"]]:
            best[rid] = r
    titles = list(best.values())
    on_shelf = [r for r in titles if r["state"] == "available"]
    out_rows = [r for r in titles if r["state"] != "available"]

    def sortk(r):
        return (r["call_number"] or "", r["title"] or "")

    # World Language first, then split the rest by shelf format.
    wl = sorted((r for r in on_shelf if _is_world_language(r)), key=sortk)
    rest = [r for r in on_shelf if not _is_world_language(r)]
    board = sorted((r for r in rest if _shelf_cat(r) == "board"), key=sortk)
    picture = sorted((r for r in rest if _shelf_cat(r) == "picture"), key=sortk)
    other = sorted((r for r in rest if _shelf_cat(r) == "other"), key=sortk)

    lines = [f"# {branch} Branch — on the shelf now\n", _gen_header(as_of),
             f"\n**{len(on_shelf)} titles on the shelf** (of {len(titles)} we track here).\n"]
    if not on_shelf:
        lines.append("\n(none right now)\n")
    for name, items in [("Board books", board), ("Picture books", picture),
                        ("Readers & holiday", other), ("Chinese / World Language", wl)]:
        if items:
            lines.append(f"\n## {name}\n")
            lines += [f"- `{r['call_number'] or ''}` {r['title']}" for r in items]
    if out_rows:
        outs = ", ".join(sorted(r["title"] or "" for r in out_rows))
        lines.append(f"\n## Not on the shelf right now (out or in-library only)\n\n{outs}\n")
    return "\n".join(lines) + "\n"


def write_all(db_path: str, outdir: str = ".") -> list[str]:
    """(Re)generate every markdown file from the DB. Returns the paths written."""
    rows, as_of = _load(db_path)
    outdir = Path(outdir)
    written = []
    (outdir / "books.md").write_text(_books_md(rows, as_of), encoding="utf-8")
    written.append(str(outdir / "books.md"))
    for branch, fname in SHELF_FILES.items():
        (outdir / fname).write_text(_branch_md(branch, rows, as_of), encoding="utf-8")
        written.append(str(outdir / fname))
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render the catalog store as markdown.")
    ap.add_argument("--matrix", action="store_true", help="print the cross-branch matrix")
    ap.add_argument("--write", action="store_true", help="regenerate all markdown files")
    ap.add_argument("--db", default="peorialib.db", help="SQLite path (default peorialib.db)")
    args = ap.parse_args(argv)
    if args.write:
        for p in write_all(args.db):
            print(f"wrote {p}")
    elif args.matrix:
        print(matrix(args.db))
    else:
        ap.error("nothing to do; pass --write or --matrix")


if __name__ == "__main__":
    main()
