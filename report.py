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

and, once bayarea_lookup.py has run at least once:
    bayarea.md        title × system overview for the Bay Area lookups
    sccl.md           \\
    sjpl.md            }  per-system, per-branch shelf-walks
    mountainview.md   /

ingest.py, library_lookup.py, and bayarea_lookup.py call the writers after every
scrape, so the files stay current automatically.
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

# Public per-record catalog pages (what a human clicks; the lookups themselves
# go through the APIs in library_lookup.py / bayarea_lookup.py).
PEORIA_CATALOG = "https://alsi.sdp.sirsi.net/client/en_US/PeoriaPL"
RECORD_URL = {
    "sccl": "https://sccl.bibliocommons.com/v2/record/{}",
    "sjpl": "https://sjpl.bibliocommons.com/v2/record/{}",
    "mvpl": "https://classiccatalog.mountainview.gov/record={}",
    "linkplus": "https://csul.iii.com/record={}",
}


def record_url(system, bib_id):
    """Catalog page for a matched remote record, or None if not linkable."""
    if not bib_id or system not in RECORD_URL:
        return None
    return RECORD_URL[system].format(bib_id)


def peoria_url(record_id):
    """RSAcat detail page for a Peoria record (WANT: rows have none)."""
    if not record_id or not str(record_id).startswith("SD_ILS:"):
        return None
    return (f"{PEORIA_CATALOG}/search/detailnonmodal/"
            f"ent:$002f$002fSD_ILS$002f0$002f{record_id}/one")


def _link(text, url):
    return f"[{text}]({url})" if url else str(text or "")


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
        lines.append(f"| {_link(title, peoria_url(key))} | {call or ''} | "
                     f"{_TYPE_LABEL[types[key]]} | " + " | ".join(cells) + " |")
    return ("\n".join(lines) + "\nLegend: Type = Board / Picture / Other (readers, holiday, "
            "world-language, or checked out at every branch so the shelf isn't shown); "
            "✓ on shelf · in-library use only ✗ out (blank = not held there); "
            "titles link to the Peoria catalog record")


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
            lines += [f"- `{r['call_number'] or ''}` "
                      f"{_link(r['title'], peoria_url(r['record_id']))}"
                      for r in items]
    if out_rows:
        outs = ", ".join(sorted(_link(r["title"] or "", peoria_url(r["record_id"]))
                                for r in out_rows))
        lines.append(f"\n## Not on the shelf right now (out or in-library only)\n\n{outs}\n")
    return "\n".join(lines) + "\n"


# --- Bay Area systems (bayarea_lookup.py) ---------------------------------------

REMOTE_SYSTEMS = {  # key -> (display name, per-system output file)
    "sccl": ("Santa Clara County Library District", "sccl.md"),
    "sjpl": ("San José Public Library", "sjpl.md"),
    "mvpl": ("Mountain View Public Library", "mountainview.md"),
    "linkplus": ("LINK+ (union catalog — request for pickup)", "linkplus.md"),
}
REMOTE_ORDER = ["sccl", "sjpl", "mvpl", "linkplus"]

# The branches the user actually visits — these lead every rendering.
# (system, branch as stored in remote_availability, column label);
# branch None = the whole system counts (Mountain View is a single building).
FAVORITES = [
    ("sccl", "Cupertino Library", "Cupertino"),
    ("sccl", "Los Altos Library", "Los Altos"),
    ("mvpl", None, "Mountain View"),
    ("sjpl", "Calabazas", "Calabazas"),
]


def _load_remote(db_path):
    """Latest remote availability + match/edition tables.

    Returns (avail_rows, bibs, titles, editions, as_of) where editions maps
    (system, record_id, bib_id) -> remote_editions row.
    """
    conn = db.open_db(db_path)
    rows = db.latest_remote_availability(conn)
    bibs = conn.execute("SELECT * FROM remote_bibs").fetchall()
    titles = {r["record_id"]: r for r in conn.execute("SELECT * FROM titles")}
    editions = {(e["system"], e["record_id"], e["bib_id"]): e
                for e in db.remote_editions(conn)}
    as_of = conn.execute("SELECT MAX(checked_at) FROM remote_availability").fetchone()[0]
    conn.close()
    return rows, bibs, titles, editions, as_of


_LANG_LABEL = {"chi": "Chinese", "fre": "French", "spa": "Spanish",
               "jpn": "Japanese", "kor": "Korean", "vie": "Vietnamese",
               "rus": "Russian", "ger": "German"}
_ED_FMT_LABEL = {"board": "board book", "audio": "audiobook",
                 "ebook": "eBook", "eaudio": "eAudiobook"}
_DIGITAL_CLASSES = ("ebook", "eaudio")


def _edition_label(ed) -> str:
    """'Spanish board book: “Oso polar…”' / 'audiobook: “Brown bear & friends”'
    / 'board book' / '' for the want's plain edition.

    kind='audio' means the record was accepted *without* a title match — it is
    its own work (a compilation that carries the story), not this book, so its
    real title must show. A translation's real title is what's printed on the
    spine you'd hunt for, so it shows too.
    """
    if ed is None:
        return ""
    parts = []
    if ed["kind"] == "translation":
        parts.append(_LANG_LABEL.get(ed["language"], ed["language"]))
    fmt = _ED_FMT_LABEL.get(ed["format_class"] or "")
    if fmt:
        parts.append(fmt)
    label = " ".join(parts)
    if ed["kind"] in ("audio", "translation") and ed["title"]:
        label = f"{label}: “{ed['title']}”" if label else f"“{ed['title']}”"
    if ed["kind"] == "translation" and (ed["orig_title"] or "").strip():
        label += f" — translation of “{ed['orig_title'].strip()}”"
    if ed["kind"] == "audio" and (ed["contents"] or "").strip():
        c = ed["contents"].strip()
        if len(c) > 300:
            c = c[:297] + "…"
        label += f" (contains: {c})"
    return label


def _title_key(titles, record_id):
    """Collapse duplicate want-list records (same book, two Peoria editions)."""
    t = titles.get(record_id)
    if t is None:
        return (record_id, "")
    return ((t["title"] or "").lower(), t["format"] or "")


def _bayarea_md(rows, bibs, titles, editions, as_of) -> str:
    # per (title-key, system): best state + branches with an available copy
    state = {}      # (tkey, system) -> 'available' | 'out' | 'reference'
    branches = {}   # (tkey, system) -> set of branches with a copy on shelf
    bstate = {}     # (tkey, system, branch) -> best state at that branch
    searched = {}   # (tkey, system) -> True if that system was searched
    matched = {}    # (tkey, system) -> True if a bib matched
    bib_of = {}     # (tkey, system) -> primary bib_id, for the cell links
    meta = {}       # tkey -> (display title, format)
    for b in bibs:
        tkey = _title_key(titles, b["record_id"])
        t = titles.get(b["record_id"])
        meta.setdefault(tkey, (t["title"] if t else b["record_id"],
                               (t["format"] if t else "") or "?"))
        searched[(tkey, b["system"])] = True
        if b["bib_id"]:
            matched[(tkey, b["system"])] = True
            bib_of.setdefault((tkey, b["system"]), b["bib_id"])
    for r in rows:
        tkey = _title_key(titles, r["record_id"])
        k = (tkey, r["system"])
        cur = state.get(k)
        if cur is None or _RANK[r["state"]] > _RANK[cur]:
            state[k] = r["state"]
        if r["state"] == "available":
            branches.setdefault(k, set()).add(r["branch"])
        bk = (tkey, r["system"], r["branch"])
        if bk not in bstate or _RANK[r["state"]] > _RANK[bstate[bk]]:
            bstate[bk] = r["state"]

    def cell(tkey, system):
        k = (tkey, system)
        url = record_url(system, bib_of.get(k))
        if k in branches:
            n = len(branches[k])
            return _link("✓" if system == "mvpl" else f"✓ {n}", url)
        # (linkplus: n counts member library systems with a copy on shelf)
        if k in matched:
            return _link("·" if state.get(k) == "reference" else "✗", url)
        if k in searched:
            return "—"
        return ""

    def fav_cell(tkey, system, branch):
        if (tkey, system) not in searched:
            return ""
        if (tkey, system) not in matched:
            return "—"
        if branch is None:
            states = [s for (tk, sy, _), s in bstate.items()
                      if tk == tkey and sy == system]
        else:
            states = [bstate.get((tkey, system, branch))]
        states = [s for s in states if s]
        if not states:
            return ""
        return _link(CELL[max(states, key=lambda s: _RANK[s])],
                     record_url(system, bib_of.get((tkey, system))))

    sys_names = [REMOTE_SYSTEMS[s][0] for s in REMOTE_ORDER]
    out = ["# Bay Area libraries — overview\n", _gen_header(as_of),
           "\nThe want-list, looked up at four Bay Area systems "
           "(`uv run bayarea_lookup.py`):\n",
           "| Key | System | In catalog | On a shelf now |", "|---|---|---|---|"]
    for s in REMOTE_ORDER:
        in_cat = sum(1 for (tk, sy) in matched if sy == s)
        on_shelf = sum(1 for (tk, sy) in branches if sy == s)
        out.append(f"| `{s}` | {REMOTE_SYSTEMS[s][0]} | {in_cat} | {on_shelf} |")
    out += ["\nPer-system shelf lists: " +
            ", ".join(f"`{REMOTE_SYSTEMS[s][1]}`" for s in REMOTE_ORDER) + ".\n",
            "\n## Your branches\n",
            "| Title | Type | " + " | ".join(lbl for _, _, lbl in FAVORITES) + " |",
            "|" + "---|" * (2 + len(FAVORITES))]
    for tkey in sorted(meta, key=lambda k: k[0]):
        title, fmt = meta[tkey]
        cells = [fav_cell(tkey, s, b) for s, b, _ in FAVORITES]
        out.append(f"| {title} | {fmt} | " + " | ".join(cells) + " |")
    out += ["\nLegend: ✓ on that shelf now · in-library use only "
            "✗ that branch's copies are all out (blank = that branch doesn't "
            "hold it) — not in that system's catalog. Marks link to the "
            "record in that catalog and cover every tracked version "
            "(board/audio/translations — breakdown in the per-system files).\n",
            "\n## Title × system\n",
            "| Title | Type | " + " | ".join(sys_names) + " |",
            "|" + "---|" * (2 + len(sys_names))]
    for tkey in sorted(meta, key=lambda k: k[0]):
        title, fmt = meta[tkey]
        cells = [cell(tkey, s) for s in REMOTE_ORDER]
        out.append(f"| {title} | {fmt} | " + " | ".join(cells) + " |")
    out.append("\nLegend: ✓ on the shelf now (SCCLD/SJPL: at that many branches) "
               "· in-library use only ✗ in the catalog but no copy on the shelf "
               "— not found in that catalog (blank = not looked up there yet). "
               "Marks link to the record in that catalog and cover every "
               "tracked version of the title.")
    return "\n".join(out) + "\n"


def _linkplus_md(rows, bibs, titles, editions, as_of) -> str:
    """LINK+ spans ~70 member systems, so this is title-centric, not per-branch."""
    srows = [r for r in rows if r["system"] == "linkplus"]
    sbibs = [b for b in bibs if b["system"] == "linkplus"]
    matched = {b["record_id"] for b in sbibs if b["bib_id"]}
    bib_by_rid = {b["record_id"]: b["bib_id"] for b in sbibs if b["bib_id"]}
    per_rec = {}
    for r in srows:
        per_rec.setdefault(r["record_id"], []).append(r)
    lines = ["# LINK+ — want-list in the union catalog\n", _gen_header(as_of),
             "\nAnything below can be **requested for pickup at a member "
             "library** (Mountain View is one). ✓ counts are library systems "
             "with a copy on the shelf right now, across every edition we "
             "track; titles link to the LINK+ record.\n",
             f"\n**{len(matched)}** of **{len(sbibs)}** titles are in LINK+.\n"]
    have, nowhere = [], []
    for rid in sorted(matched, key=lambda r: (titles[r]["title"].lower()
                                              if r in titles else r)):
        title = titles[rid]["title"] if rid in titles else rid
        tlink = _link(title, record_url("linkplus", bib_by_rid.get(rid)))
        rs = per_rec.get(rid, [])
        libs = sorted({r["branch"] for r in rs if r["state"] == "available"})
        if libs:
            shown = ", ".join(libs[:6]) + (f", +{len(libs) - 6} more"
                                           if len(libs) > 6 else "")
            have.append(f"- **{tlink}** — ✓ {len(libs)}: {shown}")
        else:
            nowhere.append(tlink)
    lines += have
    if nowhere:
        lines.append("\n## In LINK+, but no copy on any member shelf right now\n")
        lines.append(", ".join(sorted(nowhere)) + "\n")
    not_in = sorted({(titles[b["record_id"]]["title"]
                      if b["record_id"] in titles else b["record_id"])
                     for b in sbibs if not b["bib_id"]})
    if not_in:
        lines.append("\n## Not found in LINK+\n")
        lines.append(", ".join(not_in) + "\n")
    return "\n".join(lines) + "\n"


def _system_md(system, rows, bibs, titles, editions, as_of) -> str:
    if system == "linkplus":
        return _linkplus_md(rows, bibs, titles, editions, as_of)
    name, _ = REMOTE_SYSTEMS[system]
    srows = [r for r in rows if r["system"] == system]
    sbibs = [b for b in bibs if b["system"] == system]
    matched = [b for b in sbibs if b["bib_id"]]
    unmatched = [b for b in sbibs if not b["bib_id"]]

    def label(rid, bib_id):
        return _edition_label(editions.get((system, rid, bib_id)))

    # branch -> {(record_id, bib_id): best availability row} — one line per
    # tracked version (board/audio/translation), not per title
    per_branch = {}
    have_shelf = set()          # (record_id, bib_id) with a copy on a shelf
    for r in srows:
        best = per_branch.setdefault(r["branch"], {})
        key = (r["record_id"], r["bib_id"])
        cur = best.get(key)
        if cur is None or _RANK[r["state"]] > _RANK[cur["state"]]:
            best[key] = r
        if r["state"] == "available":
            have_shelf.add(key)

    all_records = {b["record_id"] for b in matched}
    shelf_records = {rid for rid, _ in have_shelf}
    # every version we track: edition rows, plus the primary bib as fallback
    # for data recorded before remote_editions existed
    all_versions = {(rid, bib) for (sy, rid, bib) in editions if sy == system}
    all_versions |= {(b["record_id"], b["bib_id"]) for b in matched}
    # digital editions have no shelf — they get their own section, not a
    # misleading spot in the "no copy on any shelf" list
    digital = {(rid, bib) for rid, bib in all_versions
               if (editions.get((system, rid, bib)) or {"format_class": ""})
               ["format_class"] in _DIGITAL_CLASSES}
    all_versions -= digital

    def named(rid, bib_id):
        t = titles[rid]["title"] if rid in titles else rid
        lab = label(rid, bib_id)
        return _link(t, record_url(system, bib_id)) + (f" ({lab})" if lab else "")

    nowhere = sorted({named(rid, bib) for rid, bib in all_versions - have_shelf})
    not_in_cat = sorted({(titles[b["record_id"]]["title"]
                          if b["record_id"] in titles else b["record_id"])
                         for b in unmatched})

    lines = [f"# {name} — want-list on the shelf now\n", _gen_header(as_of),
             f"\n**{len(all_records)}** of **{len(sbibs)}** titles are in the "
             f"catalog; **{len(shelf_records)}** have at least one copy on a "
             f"shelf right now. Titles link to the record in this catalog; "
             f"unlabeled lines are the plain edition, labels mark the other "
             f"versions we track (board book / audiobook / eBook / eAudiobook "
             f"/ translations).\n"]

    fav_names = [b for s, b, _ in FAVORITES if s == system and b]

    def branch_key(item):
        bname, best = item
        n = sum(1 for r in best.values() if r["state"] == "available")
        return (bname not in fav_names, -n, bname)

    for bname, best in sorted(per_branch.items(), key=branch_key):
        avail = [(k, r) for k, r in best.items() if r["state"] == "available"]
        if not avail:
            continue
        lines.append(f"\n## {bname} — {len(avail)} on the shelf\n")
        for (rid, bib_id), r in sorted(
                avail, key=lambda kr: (kr[1]["call_number"] or "",
                                       kr[1]["title"] or "")):
            lab = label(rid, bib_id)
            lines.append(f"- `{r['call_number'] or '?'}` "
                         f"{_link(r['title'], record_url(system, bib_id))}"
                         + (f" — {lab}" if lab else ""))
    if digital:
        lines.append("\n## Digital (eBook / eAudiobook — borrow via the "
                     "library's app; availability is a license queue, not a "
                     "shelf)\n")
        lines.append(", ".join(sorted({named(rid, bib)
                                       for rid, bib in digital})) + "\n")
    if nowhere:
        lines.append("\n## In the catalog, but no copy on any shelf right now\n")
        lines.append(", ".join(nowhere) + "\n")
    if not_in_cat:
        lines.append("\n## Not found in this catalog\n")
        lines.append(", ".join(not_in_cat) + "\n")
    return "\n".join(lines) + "\n"


def write_bayarea(db_path: str, outdir: str = ".") -> list[str]:
    """(Re)generate the Bay Area markdown. No-op (returns []) before any lookup."""
    rows, bibs, titles, editions, as_of = _load_remote(db_path)
    if not bibs:
        return []
    outdir = Path(outdir)
    written = []
    (outdir / "bayarea.md").write_text(
        _bayarea_md(rows, bibs, titles, editions, as_of), encoding="utf-8")
    written.append(str(outdir / "bayarea.md"))
    for system, (_, fname) in REMOTE_SYSTEMS.items():
        if any(b["system"] == system for b in bibs):
            (outdir / fname).write_text(
                _system_md(system, rows, bibs, titles, editions, as_of),
                encoding="utf-8")
            written.append(str(outdir / fname))
    return written


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
    written += write_bayarea(db_path, outdir)
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
