"""SQLite persistence for Peoria Public Library catalog scrapes.

A growing time-series of what was on which branch's shelf, and when. Deliberately
generous with columns (the point is to keep more than we strictly need). Pure
stdlib `sqlite3`, no dependencies.

Two writers feed the same helpers:
  * library_lookup.py     — the live Playwright scraper (writes when --db is set)
  * ingest.py             — loads JSON captured via Claude-in-Chrome

Schema (all `CREATE TABLE IF NOT EXISTS`, so init is idempotent):
  titles            one row per catalog record (stable-ish metadata)
  scrapes           one row per lookup event
  availability      one row per branch copy per check  (the time-series core)
  search_snapshots  per-search Peoria-wide availability signal
"""
from __future__ import annotations

import re
import sqlite3

# --- branch normalization -------------------------------------------------------

# The six Peoria Public Library branches, as they appear after the "Peoria PL - "
# prefix in the catalog's Library column.
PEORIA_BRANCHES = ("Main St", "Lakeview", "Lincoln", "McClure", "North", "Outreach")


def branch_norm(library_text: str):
    """Normalize a Library-column value to (branch, is_peoria).

    Accepts either the full catalog form ('Peoria PL - North') or a bare branch
    name ('North') — the live scraper's DETAIL_JS already strips the prefix.
    Consortium libraries return (name, 0).
    """
    s = re.sub(r"\s+", " ", (library_text or "")).strip()
    m = re.match(r"^Peoria PL\s*-\s*(.+)$", s, re.IGNORECASE)
    if m:
        return m.group(1).strip(), 1
    if s in PEORIA_BRANCHES:
        return s, 1
    return s, 0


# --- classification (mirrors library_lookup.classify, kept standalone) ----------

_OUT_MARKERS = ("checked out", "transfer", "transit", "on hold", "in repair",
                "lost", "missing", "damaged", "claimed", "billed", "on order")
_NONCIRC_MARKERS = ("non-circulating", "workroom", "reference", "staff", "display")


def classify(status: str) -> str:
    s = (status or "").lower()
    if any(m in s for m in _OUT_MARKERS):
        return "out"
    if any(m in s for m in _NONCIRC_MARKERS):
        return "reference"
    return "available"


def format_guess(call_number: str, material_type: str = "", status: str = "") -> str:
    """Best-effort board/picture/reader/other from the cues the catalog exposes."""
    blob = " ".join((call_number or "", material_type or "", status or "")).lower()
    if "board" in blob or re.search(r"\bbb\b|/bbk|brdbk|bdbk|e brd", blob):
        return "board"
    if "reader" in blob:
        return "reader"
    if "picture" in blob or re.search(r"\bjp\b|\be(c|z)?\b|\bp\b", blob):
        return "picture"
    return "other"


# --- schema ---------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    record_id   TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    author      TEXT,
    year        TEXT,
    isbns       TEXT,
    publisher   TEXT,
    phys_desc   TEXT,
    summary     TEXT,
    audience    TEXT,
    format      TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrapes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- 'search' | 'detail'
    query       TEXT,
    source      TEXT,                    -- 'library_lookup' | 'claude-in-chrome' | ...
    profile     TEXT
);

CREATE TABLE IF NOT EXISTS availability (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_id     INTEGER REFERENCES scrapes(id),
    record_id     TEXT REFERENCES titles(record_id),
    title         TEXT,
    branch        TEXT NOT NULL,
    is_peoria     INTEGER NOT NULL,
    call_number   TEXT,
    material_type TEXT,
    status_raw    TEXT,
    state         TEXT,                  -- 'available' | 'out' | 'reference'
    checked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_id   INTEGER REFERENCES scrapes(id),
    record_id   TEXT REFERENCES titles(record_id),
    query       TEXT,
    call_number TEXT,
    local_count INTEGER,
    avail_text  TEXT,
    checked_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_avail_record  ON availability(record_id);
CREATE INDEX IF NOT EXISTS ix_avail_branch  ON availability(branch);
CREATE INDEX IF NOT EXISTS ix_avail_checked ON availability(checked_at);
"""

_TITLE_FIELDS = ("author", "year", "isbns", "publisher", "phys_desc",
                 "summary", "audience", "format")


def open_db(path: str) -> sqlite3.Connection:
    """Connect, enable FKs, and ensure the schema exists (idempotent)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- writes ---------------------------------------------------------------------

def record_scrape(conn, kind: str, checked_at: str, query: str = None,
                  source: str = None, profile: str = None) -> int:
    cur = conn.execute(
        "INSERT INTO scrapes (checked_at, kind, query, source, profile) "
        "VALUES (?,?,?,?,?)",
        (checked_at, kind, query, source, profile),
    )
    return cur.lastrowid


def upsert_title(conn, record_id: str, title: str, checked_at: str, meta: dict = None):
    """Insert a title or update its metadata + last_seen; keeps first_seen."""
    meta = meta or {}
    row = conn.execute(
        "SELECT record_id FROM titles WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        cols = ["record_id", "title", "first_seen", "last_seen"] + list(_TITLE_FIELDS)
        vals = [record_id, title, checked_at, checked_at] + [meta.get(f) for f in _TITLE_FIELDS]
        conn.execute(
            f"INSERT INTO titles ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
    else:
        # Update title + any provided metadata fields, bump last_seen. COALESCE keeps
        # an existing value when this scrape didn't supply one.
        sets = ["title = ?", "last_seen = ?"]
        vals = [title, checked_at]
        for f in _TITLE_FIELDS:
            if meta.get(f) is not None:
                sets.append(f"{f} = ?")
                vals.append(meta[f])
        vals.append(record_id)
        conn.execute(f"UPDATE titles SET {', '.join(sets)} WHERE record_id = ?", vals)


def add_availability(conn, scrape_id: int, record_id: str, title: str,
                     holdings, checked_at: str):
    """holdings: iterable of dicts with branch/call_number/status/material_type."""
    for h in holdings:
        branch, is_peoria = branch_norm(h.get("branch", ""))
        status = (h.get("status") or "").strip()
        conn.execute(
            "INSERT INTO availability (scrape_id, record_id, title, branch, is_peoria, "
            "call_number, material_type, status_raw, state, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (scrape_id, record_id, title, branch, is_peoria,
             h.get("call_number"), h.get("material_type"), status,
             classify(status), checked_at),
        )


def add_search_snapshot(conn, scrape_id, record_id, query, call_number,
                        local_count, avail_text, checked_at):
    conn.execute(
        "INSERT INTO search_snapshots (scrape_id, record_id, query, call_number, "
        "local_count, avail_text, checked_at) VALUES (?,?,?,?,?,?,?)",
        (scrape_id, record_id, query, call_number, local_count, avail_text, checked_at),
    )


# --- reads ----------------------------------------------------------------------

def latest_availability(conn, is_peoria_only: bool = True):
    """Most-recent availability row per (record_id, branch). Returns sqlite3.Row list."""
    where = "WHERE is_peoria = 1" if is_peoria_only else ""
    return conn.execute(
        f"""
        SELECT a.* FROM availability a
        JOIN (
            SELECT record_id, branch, MAX(checked_at) AS mx
            FROM availability {where}
            GROUP BY record_id, branch
        ) last
        ON a.record_id = last.record_id AND a.branch = last.branch
           AND a.checked_at = last.mx
        """
    ).fetchall()
