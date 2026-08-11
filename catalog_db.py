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

Remote systems (the same want-list looked up at other libraries — see
bayarea_lookup.py):
  remote_bibs          which remote catalog record we matched each title to
  remote_availability  one row per branch copy per check, tagged with the system
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

CREATE TABLE IF NOT EXISTS remote_bibs (
    system      TEXT NOT NULL,            -- 'sccl' | 'sjpl' | 'mvpl'
    record_id   TEXT NOT NULL REFERENCES titles(record_id),
    bib_id      TEXT,                     -- remote catalog id; NULL = no match found
    title       TEXT,
    author      TEXT,
    format      TEXT,                     -- remote catalog's format label
    year        TEXT,
    match_score REAL,
    checked_at  TEXT NOT NULL,
    PRIMARY KEY (system, record_id)
);

CREATE TABLE IF NOT EXISTS remote_editions (
    system      TEXT NOT NULL,
    record_id   TEXT NOT NULL REFERENCES titles(record_id),
    bib_id      TEXT NOT NULL,
    title       TEXT,
    author      TEXT,
    format      TEXT,                     -- remote catalog's format label
    format_class TEXT,                    -- book | picture | board | audio | ebook | eaudio
    language    TEXT,                     -- 'eng' | 'spa' | 'chi' | … | NULL
    kind        TEXT,                     -- primary | edition | translation | audio
    match_score REAL,
    checked_at  TEXT NOT NULL,
    contents    TEXT,                     -- compilation contents note ('' = fetched, none)
    orig_title  TEXT,                     -- translation's original title ('' = fetched, none)
    PRIMARY KEY (system, record_id, bib_id)
);

CREATE TABLE IF NOT EXISTS remote_availability (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_id   INTEGER REFERENCES scrapes(id),
    system      TEXT NOT NULL,
    record_id   TEXT REFERENCES titles(record_id),
    bib_id      TEXT,
    title       TEXT,
    branch      TEXT NOT NULL,            -- branch (sccl/sjpl) or shelf location (mvpl)
    collection  TEXT,
    call_number TEXT,
    status_raw  TEXT,
    state       TEXT,                     -- 'available' | 'out' | 'reference'
    checked_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ravail_sys_rec ON remote_availability(system, record_id);
CREATE INDEX IF NOT EXISTS ix_ravail_checked ON remote_availability(checked_at);
"""

_TITLE_FIELDS = ("author", "year", "isbns", "publisher", "phys_desc",
                 "summary", "audience", "format")


def open_db(path: str) -> sqlite3.Connection:
    """Connect, enable FKs, and ensure the schema exists (idempotent)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # the Bay Area lookup runs its systems in parallel threads, one connection
    # each: WAL lets readers pass the writer, and a generous busy timeout makes
    # concurrent writers queue instead of throwing 'database is locked'
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # light migration: columns added after the table first shipped
    cols = {r[1] for r in conn.execute("PRAGMA table_info(remote_editions)")}
    for col in ("contents", "orig_title"):
        if col not in cols:
            conn.execute(f"ALTER TABLE remote_editions ADD COLUMN {col} TEXT")
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


def upsert_remote_bib(conn, system: str, record_id: str, checked_at: str,
                      bib: dict = None, match_score: float = None):
    """Record which remote bib a title matched (bib=None → searched, nothing found)."""
    bib = bib or {}
    conn.execute(
        "INSERT OR REPLACE INTO remote_bibs (system, record_id, bib_id, title, "
        "author, format, year, match_score, checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (system, record_id, bib.get("bib_id"), bib.get("title"), bib.get("author"),
         bib.get("format"), bib.get("year"), match_score, checked_at),
    )


def replace_remote_editions(conn, system: str, record_id: str, editions,
                            checked_at: str):
    """Replace the full edition set (all matched versions incl. the primary).

    A re-lookup supersedes the previous set wholesale, so a corrected match or a
    vanished edition can't leave a stale row behind.
    """
    conn.execute("DELETE FROM remote_editions WHERE system = ? AND record_id = ?",
                 (system, record_id))
    for e in editions:
        if not e.get("bib_id"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO remote_editions (system, record_id, bib_id, "
            "title, author, format, format_class, language, kind, match_score, "
            "checked_at, contents, orig_title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (system, record_id, e["bib_id"], e.get("title"),
             ", ".join(e.get("authors") or []) or None, e.get("format"),
             e.get("format_class"), e.get("language"), e.get("kind"),
             e.get("match_score"), checked_at, e.get("contents"),
             e.get("orig_title")),
        )


def unenriched_editions(conn):
    """Compilation/translation editions whose record details aren't fetched yet."""
    return conn.execute(
        "SELECT system, record_id, bib_id, kind FROM remote_editions "
        "WHERE kind IN ('audio', 'translation') "
        "AND contents IS NULL AND orig_title IS NULL").fetchall()


def delete_remote_edition(conn, system: str, record_id: str, bib_id: str):
    """Drop one tracked version (a verification failure); its availability
    rows stop being 'latest' the moment the edition row is gone."""
    conn.execute(
        "DELETE FROM remote_editions "
        "WHERE system = ? AND record_id = ? AND bib_id = ?",
        (system, record_id, bib_id))


def set_edition_details(conn, system: str, record_id: str, bib_id: str,
                        contents: str, orig_title: str):
    """'' (not NULL) marks 'fetched, record has none' so we don't refetch."""
    conn.execute(
        "UPDATE remote_editions SET contents = ?, orig_title = ? "
        "WHERE system = ? AND record_id = ? AND bib_id = ?",
        (contents or "", orig_title or "", system, record_id, bib_id))


def remote_editions(conn):
    """All current edition rows, for labeling availability in reports."""
    return conn.execute("SELECT * FROM remote_editions").fetchall()


def add_remote_availability(conn, scrape_id: int, system: str, record_id: str,
                            bib_id: str, title: str, items, checked_at: str):
    """items: iterable of dicts with branch/collection/call_number/status/state."""
    for it in items:
        conn.execute(
            "INSERT INTO remote_availability (scrape_id, system, record_id, bib_id, "
            "title, branch, collection, call_number, status_raw, state, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (scrape_id, system, record_id, bib_id, title,
             (it.get("branch") or "").strip() or "?", it.get("collection"),
             it.get("call_number"), it.get("status"), it.get("state"), checked_at),
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


def latest_remote_availability(conn, system: str = None):
    """The current footprint of each title at each system.

    Rows come only from the newest scrape per (system, record_id) — an older
    scrape's branches must not linger once a re-scrape has replaced them — and
    only when they belong to a currently-matched bib (the primary in remote_bibs
    or any current remote_editions row), so availability recorded for a
    since-corrected mismatch disappears with the correction.
    """
    where = "WHERE system = ?" if system else ""
    args = (system,) if system else ()
    return conn.execute(
        f"""
        SELECT a.* FROM remote_availability a
        JOIN (
            SELECT system, record_id, MAX(checked_at) AS mx
            FROM remote_availability {where}
            GROUP BY system, record_id
        ) last
        ON a.system = last.system AND a.record_id = last.record_id
           AND a.checked_at = last.mx
        WHERE EXISTS (SELECT 1 FROM remote_bibs rb
                      WHERE rb.system = a.system AND rb.record_id = a.record_id
                        AND rb.bib_id = a.bib_id)
           OR EXISTS (SELECT 1 FROM remote_editions re
                      WHERE re.system = a.system AND re.record_id = a.record_id
                        AND re.bib_id = a.bib_id)
        """,
        args,
    ).fetchall()
