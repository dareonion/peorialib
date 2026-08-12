#!/usr/bin/env python3
"""Render the catalog store (catalog_db) into markdown — the ONLY way the .md files
are produced. They are generated artifacts of `shelfwalk.db`; never hand-edit them.

    uv run report.py --write      # (re)generate all markdown from the DB
    uv run report.py --matrix     # just print the cross-branch matrix to stdout

`--write` produces, from the Bay Area lookups:
    bayarea.md        title × system overview (+ a "your branches" matrix)
    sccl.md           \\
    sjpl.md            }  per-system, per-branch shelf-walks
    mountainview.md   /
    linkplus.md       union catalog, title-centric (70 systems, no shelf-walk)

and, from the retired Peoria scrape data:
    books.md      overview: per-branch counts + the full title-x-branch matrix
    north.md      \\
    lakeview.md    }  per-branch "on the shelf now" shelf-walks
    main.md       /

bayarea_lookup.py (and, historically, ingest.py / library_lookup.py) call the
writers after every scrape, so the files stay current automatically.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path

import catalog_db as db

# All Peoria branches, in the order they appear in the matrix (user's branches first).
BRANCH_ORDER = ["North", "Lakeview", "Main St", "Lincoln", "McClure", "Outreach"]
# Per-branch shelf-walk files we generate (the branches the user actually visits).
SHELF_FILES = {"North": "north.md", "Lakeview": "lakeview.md", "Main St": "main.md"}
CELL = {"available": "✓", "reference": "·", "out": "✗"}
_RANK = {"available": 3, "reference": 2, "out": 1}

BANNER = ("<!-- AUTO-GENERATED from shelfwalk.db by report.py — do not edit by hand. "
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
            f"_Auto-generated from `shelfwalk.db` — data as of **{as_of or 'n/a'}**. "
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
            lines += ["| Call # | Title |", "|---|---|"]
            lines += [f"| `{r['call_number'] or ''}` | "
                      f"{_link(r['title'], peoria_url(r['record_id']))} |"
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
    ("sjpl", "West Valley", "West Valley"),
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


def _stem_key(s) -> str:
    """Pre-subtitle stem, folded flat — for 'is this really the same title?'."""
    s = re.split(r"[:：=/]", s or "")[0]
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9一-鿿]+", "", s.lower())


def _edition_label(ed, want_title: str = None) -> str:
    """'Spanish board book: “Oso polar…”' / 'audiobook: “Brown bear & friends”'
    / 'board book' / '' for the want's plain edition.

    kind='audio' means the record was accepted *without* a title match — it is
    its own work (a compilation that carries the story), not this book, so its
    real title must show. A translation's real title is what's printed on the
    spine you'd hunt for, so it shows too. And an 'edition' whose stem doesn't
    match the want's discloses its title as well — nothing may sail under the
    want's name unless it really carries it.
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
    show_title = (ed["kind"] in ("audio", "translation")
                  or (ed["kind"] == "edition" and want_title
                      and _stem_key(ed["title"]) != _stem_key(want_title)))
    if show_title and ed["title"]:
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
    """Collapse duplicate want-list records (same book, two Peoria editions).

    Keyed on the title alone: the two Peoria records for *The Very Hungry
    Caterpillar* differ only in shelf format, and keeping format in the key
    listed the book twice in every rendering.
    """
    t = titles.get(record_id)
    if t is None:
        return (record_id, "")
    return ((t["title"] or "").lower(),)


# --- bibliographic details (remote_editions.details, set by the enrich pass) ---

_DETAIL_PREF = {"sccl": 0, "sjpl": 1, "mvpl": 2, "linkplus": 3}


def _merge_details(editions, titles) -> dict:
    """tkey -> one merged details dict. Catalogs describe the same book with
    different completeness, so take each field from the first record that has
    it, preferring the primary edition and the richest catalog."""
    per = {}
    for (system, rid, _bib), e in editions.items():
        try:
            d = json.loads(e["details"] or "{}")
        except (TypeError, ValueError):
            continue
        if not d:
            continue
        rank = (0 if e["kind"] == "primary" else 1, _DETAIL_PREF.get(system, 9))
        per.setdefault(_title_key(titles, rid), []).append((rank, d))
    out = {}
    for tkey, items in per.items():
        merged = {}
        for _rank, d in sorted(items, key=lambda x: x[0]):
            for k, v in d.items():
                if v and k not in merged:
                    merged[k] = v
        out[tkey] = merged
    return out


def _first(val):
    """details values are a string or a list of strings."""
    if isinstance(val, list):
        return val[0] if val else ""
    return val or ""


def _all(val) -> list:
    if isinstance(val, list):
        return [v for v in val if v]
    return [val] if val else []


# Reading-program noise that must never be read as an age range: 'AR LG 2.0
# 0.5', 'RC K-2 1.5 1', 'Guided reading level: I', '440 Lexile'.
_NOT_AGE = re.compile(r"lexile|quiz|guided|reading counts|reader|\bRC\b|\bAR\b", re.I)
_AGES_EXPLICIT = re.compile(r"\bages?\b[\s:]*(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})", re.I)
_AGES_OPEN = re.compile(r"\bages?\b[\s:]*(\d{1,2})\s*\+", re.I)
# a bare range leading the value: '2-5 Brodart', '04-06', '3-7 years'
_AGES_BARE = re.compile(r"^\D{0,4}(\d{1,2})\s*(?:-|–)\s*(\d{1,2})\b")
_PREK = re.compile(r"pre-?k|pre-?s(?:chool)?\b|toddler|\bbaby\b|^P\s*-\s*\w", re.I)
_GRADES = re.compile(r"\b([K\d]{1,2})\s*(?:-|–|to)\s*([K\d]{1,2})\b", re.I)
# 'AD420L Lexile', 'AD 280 Lexile', '120 Lexile' (NP/BR carry no number)
_LEXILE = re.compile(r"\b(AD|HL|BR|IG|NC)?\s?(\d{2,4})\s*L?\b\s*Lexile", re.I)


def _ages(det) -> str:
    """Compact reading-level cell, best form first: an age range, else a
    preschool band, else grades, else a Lexile.

    The catalogs record audience a dozen ways ('Ages 3-7', '2-5 Brodart',
    'Pre-K to 1', 'K-3 Medialog', 'AD 280 Lexile'), so each value is tried
    against every shape rather than one regex over a joined blob.
    """
    best = {}
    for raw in _all(det.get("audience")) + _all(det.get("reading_program")):
        s = (raw or "").strip()
        if not s:
            continue
        if not _NOT_AGE.search(s):
            m = _AGES_EXPLICIT.search(s) or _AGES_BARE.match(s)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if 0 <= lo < hi <= 18:
                    best.setdefault(0, f"Ages {lo}-{hi}")
                    continue
            m = _AGES_OPEN.search(s)
            if m:
                best.setdefault(0, f"Ages {m.group(1)}+")
                continue
            if _PREK.search(s):
                best.setdefault(1, "PreK")
                continue
            m = _GRADES.search(s)
            if m and re.search(r"grade|^\s*K", s, re.I):
                best.setdefault(2, f"Gr {m.group(1).upper()}-{m.group(2).upper()}")
                continue
        m = _LEXILE.search(s)
        if m:
            best.setdefault(3, f"{(m.group(1) or '').upper()}{m.group(2)}L")
    return best[min(best)] if best else ""


_LOCAL_SYSTEMS = ("sccl", "sjpl", "mvpl")   # you can walk in; LINK+ you request


def _todo_md(meta, matched, bib_of, bstate, branches, titles, editions) -> list:
    """The actionable ladder: nothing to do (it's on a favorite shelf) → place a
    hold → request through LINK+ → buy. Availability alone doesn't say which,
    so this is the part of the report you act on."""
    fav_keys = [(s, b) for s, b, _ in FAVORITES]
    digital = set()     # (tkey, system) that own it only as eBook/eAudiobook
    physical = set()
    for (system, rid, bib), e in editions.items():
        k = (_title_key(titles, rid), system)
        (digital if (e["format_class"] or "") in _DIGITAL_CLASSES
         else physical).add(k)

    hold, viaplus, buy, digital_only = [], [], [], []
    for tkey in sorted(meta, key=lambda k: k[0]):
        title, _fmt = meta[tkey]
        on_fav = any(bstate.get((tkey, s, b)) == "available"
                     if b else any(st == "available"
                                   for (tk, sy, _br), st in bstate.items()
                                   if tk == tkey and sy == s)
                     for s, b in fav_keys)
        if on_fav:
            continue                       # already in the shelf-walk files
        owns = [s for s in _LOCAL_SYSTEMS if (tkey, s) in matched]
        if owns:
            if not any((tkey, s) in physical for s in owns):
                digital_only.append(_link(title, record_url(
                    owns[0], bib_of.get((tkey, owns[0])))))
                continue
            where = ", ".join(
                _link(REMOTE_SYSTEMS[s][0].split(" (")[0],
                      record_url(s, bib_of.get((tkey, s))))
                for s in owns if (tkey, s) in physical)
            # a copy sitting on some other branch's shelf travels fastest
            elsewhere = sorted({br for s in owns
                                for (tk, sy, br), st in bstate.items()
                                if tk == tkey and sy == s and st == "available"
                                and (s, br) not in fav_keys
                                and (s, None) not in fav_keys})
            speed = (f"on the shelf at {', '.join(elsewhere[:2])}"
                     f"{f' +{len(elsewhere) - 2}' if len(elsewhere) > 2 else ''}"
                     if elsewhere else "every copy out — hold and wait")
            # the title stays plain — "Owned by" carries a link per system
            hold.append(f"| {title} | {where} | {speed} |")
        elif (tkey, "linkplus") in matched:
            n = len(branches.get((tkey, "linkplus"), ()))
            viaplus.append(f"| {_link(title, record_url('linkplus', bib_of.get((tkey, 'linkplus'))))} "
                           f"| {'✓ ' + str(n) if n else 'all out'} |")
        else:
            isbns = ""
            for rid, t in titles.items():
                if _title_key(titles, rid) == tkey and t["isbns"]:
                    isbns = ", ".join(re.findall(r"[0-9Xx]{10,13}", t["isbns"]))
                    break
            buy.append(f"| {title} | {isbns} |")

    out = ["\n## To do\n",
           f"**{len(hold)}** to hold · **{len(viaplus)}** to request through "
           f"LINK+ · **{len(buy)}** to buy. Everything else is either on a "
           f"favorite branch's shelf right now (see the per-system files) or "
           f"already covered.\n"]
    if hold:
        out += ["\n### Place a hold\n",
                "Your systems own these, but no copy is on a favorite "
                "branch's shelf right now.\n",
                "| Title | Owned by | Speed |", "|---|---|---|"] + hold
    if viaplus:
        out += ["\n### Request through LINK+\n",
                "No local system has these; a LINK+ member does — request for "
                "pickup at Mountain View. ✓ = member systems with a copy on a "
                "shelf now.\n",
                "| Title | On a shelf |", "|---|---|"] + viaplus
    if buy:
        out += ["\n### Buy\n",
                "In no catalog here — not borrowable, even by request.\n",
                "| Title | ISBN |", "|---|---|"] + buy
    if digital_only:
        out += ["\n### Digital only\n",
                "Owned locally only as eBook/eAudiobook — borrow in the "
                "library's app: " + ", ".join(digital_only) + "\n"]
    return out


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
            ", ".join(f"`{REMOTE_SYSTEMS[s][1]}`" for s in REMOTE_ORDER) +
            "; per-title bibliographic detail: `titles.md`.\n"]
    out += _todo_md(meta, matched, bib_of, bstate, branches, titles, editions)
    out += ["\n## Your branches\n",
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
            have.append(f"| {tlink} | ✓ {len(libs)} | {shown} |")
        else:
            nowhere.append(tlink)
    if have:
        lines += ["| Title | On a shelf | Member systems |", "|---|---|---|"]
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

    det_by_key = _merge_details(editions, titles)

    def label(rid, bib_id):
        want = titles[rid]["title"] if rid in titles else None
        return _edition_label(editions.get((system, rid, bib_id)), want)

    def ages(rid):
        return _ages(det_by_key.get(_title_key(titles, rid), {}))

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

    def kind_rank(rid, bib_id):
        ed = editions.get((system, rid, bib_id))
        return 0 if ed and ed["kind"] == "primary" else 1

    def trans_note(bib_id, rids, winner_rid):
        """A collapsed claim's translation info survives on the winning line:
        Bonsoir Lune keeps 'translation of “Goodnight moon”' from the
        Goodnight moon row it absorbed."""
        wed = editions.get((system, winner_rid, bib_id))
        if wed and wed["kind"] == "translation":
            return ""       # its own label already says so
        for rid in rids:
            if rid == winner_rid:
                continue
            ed = editions.get((system, rid, bib_id))
            if ed and ed["kind"] == "translation":
                orig = (ed["orig_title"] or "").strip() or \
                       (titles[rid]["title"] if rid in titles else "")
                if orig:
                    return f"translation of “{orig}”"
        return ""

    def dedupe_versions(pairs):
        """(linked title, version label) rows — one per bib and per rendered
        text. Several want rows can claim one record (Bonsoir Lune is both
        its own want and Goodnight moon's French edition) — the row that owns
        it as primary wins, keeping the absorbed claim's translation note;
        and several bibs of the same printing differ only in their link,
        which reads as a duplicate too."""
        by_bib = {}
        for rid, bib in sorted(pairs, key=lambda p: (kind_rank(*p), p[1])):
            by_bib.setdefault(bib, []).append(rid)
        out = {}
        for bib, rids in by_bib.items():
            rid = rids[0]
            t = titles[rid]["title"] if rid in titles else rid
            lab = label(rid, bib)
            note = trans_note(bib, rids, rid)
            if note:
                lab = f"{lab} — {note}" if lab else note
            link = _link(t, record_url(system, bib))
            out.setdefault(re.sub(r"\]\([^)]*\)", "]", f"{link}|{lab}"),
                           (link, lab))
        return sorted(out.values())

    nowhere = dedupe_versions(all_versions - have_shelf)
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
        # one line per record on the shelf: when several want rows claim the
        # same bib (Bonsoir Lune is both its own want and Goodnight moon's
        # French edition), the row that owns it as primary wins; lines that
        # still render identically (two same-shelf printings) collapse too
        by_bib, claims = {}, {}
        for (rid, bib_id), r in avail:
            claims.setdefault(bib_id, []).append(rid)
            cur = by_bib.get(bib_id)
            if cur is None or kind_rank(rid, bib_id) < kind_rank(*cur[0]):
                by_bib[bib_id] = ((rid, bib_id), r)
        entries, seen_txt = [], set()
        for (rid, bib_id), r in sorted(
                by_bib.values(), key=lambda kr: (kr[1]["call_number"] or "",
                                                 kr[1]["title"] or "",
                                                 kr[0][1])):
            lab = label(rid, bib_id)
            note = trans_note(bib_id, claims[bib_id], rid)
            if note:
                lab = f"{lab} — {note}" if lab else note
            row = (f"| `{r['call_number'] or '?'}` | "
                   f"{_link(r['title'], record_url(system, bib_id))} | {lab} "
                   f"| {ages(rid)} |")
            key = re.sub(r"\]\([^)]*\)", "]", row)
            if key in seen_txt:
                continue
            seen_txt.add(key)
            entries.append(row)
        lines.append(f"\n## {bname} — {len(entries)} on the shelf\n")
        lines += ["| Call # | Title | Version | Ages |", "|---|---|---|---|"]
        lines += entries
    if digital:
        lines.append("\n## Digital (eBook / eAudiobook — borrow via the "
                     "library's app; availability is a license queue, not a "
                     "shelf)\n")
        lines += ["| Title | Version |", "|---|---|"]
        lines += [f"| {link} | {lab} |" for link, lab in dedupe_versions(digital)]
    if nowhere:
        lines.append("\n## In the catalog, but no copy on any shelf right now\n")
        lines += ["| Title | Version |", "|---|---|"]
        lines += [f"| {link} | {lab} |" for link, lab in nowhere]
    if not_in_cat:
        lines.append("\n## Not found in this catalog\n")
        lines.append(", ".join(not_in_cat) + "\n")
    return "\n".join(lines) + "\n"


def _titles_md(bibs, titles, editions, as_of) -> str:
    """Per-title bibliographic reference — the enrichment pass's details, which
    the shelf lists have no room for (what it's about, what age it's aimed at,
    what it won, what to buy)."""
    det_by_key = _merge_details(editions, titles)
    seen, rows = set(), []
    for b in sorted(bibs, key=lambda b: (titles[b["record_id"]]["title"].lower()
                                         if b["record_id"] in titles else "")):
        rid = b["record_id"]
        tkey = _title_key(titles, rid)
        if tkey in seen or rid not in titles:
            continue
        seen.add(tkey)
        t = titles[rid]
        det = det_by_key.get(tkey, {})
        summary = _first(det.get("summary"))
        if len(summary) > 220:
            summary = summary[:217] + "…"
        isbn = _first(det.get("isbn")) or (t["isbns"] or "")
        isbn = re.sub(r"\s*\([^)]*\)", "", isbn).strip()
        rows.append(f"| {t['title']} | {t['author'] or ''} | {_ages(det)} | "
                    f"{isbn} | {_first(det.get('awards'))} | {summary} |")
    n_det = sum(1 for k in seen if det_by_key.get(k))
    return "\n".join(
        ["# Want-list — book details\n", _gen_header(as_of),
         f"\nBibliographic detail for the **{len(rows)}** tracked titles "
         f"({n_det} with catalog details fetched), merged across systems by "
         f"the `--enrich` pass. Ages come from the catalogs' audience notes, "
         f"so a Lexile (`AD420L`) or grade band appears where no age range "
         f"was recorded, and blanks mean the record says nothing.\n",
         "| Title | Author | Ages | ISBN | Awards | Summary |",
         "|---|---|---|---|---|---|"] + rows) + "\n"


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
    (outdir / "titles.md").write_text(
        _titles_md(bibs, titles, editions, as_of), encoding="utf-8")
    written.append(str(outdir / "titles.md"))
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
    ap.add_argument("--db", default="shelfwalk.db", help="SQLite path (default shelfwalk.db)")
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
