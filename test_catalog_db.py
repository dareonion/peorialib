"""Tests for catalog_db (the SQLite store) — all against in-memory sqlite, no browser.

Run:  uv run pytest -q      (or: uv run python test_catalog_db.py)
"""
from __future__ import annotations

import catalog_db as db


def _mem():
    return db.open_db(":memory:")


def test_schema_init_idempotent():
    conn = _mem()
    conn.executescript(db.SCHEMA)  # running it again must not error
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"titles", "scrapes", "availability", "search_snapshots"} <= tables


def test_branch_norm():
    assert db.branch_norm("Peoria PL - North") == ("North", 1)
    assert db.branch_norm("Peoria PL - Main St") == ("Main St", 1)
    assert db.branch_norm("North") == ("North", 1)          # bare Peoria name
    assert db.branch_norm("Alpha Park PLD") == ("Alpha Park PLD", 0)


def test_format_guess():
    assert db.format_guess("JP BOY", "Book", "Juvenile Board Book") == "board"
    assert db.format_guess("JP CRO", "Book", "Juvenile Reader") == "reader"
    assert db.format_guess("JP BRO", "Book", "Juvenile Picture Book") == "picture"
    assert db.format_guess("821 SNY", "Book", "Adult") == "other"


def test_classify():
    assert db.classify("Juvenile Board Book") == "available"
    assert db.classify("Checked Out") == "out"
    assert db.classify("Being transferred between libraries") == "out"
    assert db.classify("Children's Workroom") == "reference"


def test_record_and_query_availability():
    conn = _mem()
    db.upsert_title(conn, "SD_ILS:1", "Moo, Baa, La La La!", "2026-07-26T12:00:00",
                    {"author": "Boynton, Sandra", "format": "board"})
    sid = db.record_scrape(conn, "detail", "2026-07-26T12:00:00",
                           query="moo baa", source="test")
    db.add_availability(conn, sid, "SD_ILS:1", "Moo, Baa, La La La!", [
        {"branch": "Peoria PL - North", "call_number": "JP BOY", "status": "Juvenile Board Book"},
        {"branch": "Peoria PL - Lakeview", "call_number": "JP BOY", "status": "Checked Out"},
        {"branch": "Alpha Park PLD", "call_number": "E BOY", "status": "Checked Out"},
    ], "2026-07-26T12:00:00")
    conn.commit()

    rows = conn.execute("SELECT branch, is_peoria, state FROM availability "
                        "ORDER BY branch").fetchall()
    by_branch = {r["branch"]: (r["is_peoria"], r["state"]) for r in rows}
    assert by_branch["North"] == (1, "available")
    assert by_branch["Lakeview"] == (1, "out")
    assert by_branch["Alpha Park PLD"] == (0, "out")


def test_upsert_bumps_last_seen_without_duplicating():
    conn = _mem()
    db.upsert_title(conn, "SD_ILS:9", "Dear Zoo", "2026-07-01T00:00:00")
    db.upsert_title(conn, "SD_ILS:9", "Dear Zoo", "2026-07-26T00:00:00",
                    {"author": "Campbell, Rod"})
    rows = conn.execute("SELECT first_seen, last_seen, author FROM titles "
                        "WHERE record_id='SD_ILS:9'").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == "2026-07-01T00:00:00"
    assert rows[0]["last_seen"] == "2026-07-26T00:00:00"
    assert rows[0]["author"] == "Campbell, Rod"


def test_latest_availability_returns_newest_per_branch():
    conn = _mem()
    db.upsert_title(conn, "SD_ILS:2", "Press Here", "2026-07-20T00:00:00")
    s1 = db.record_scrape(conn, "detail", "2026-07-20T00:00:00", source="test")
    db.add_availability(conn, s1, "SD_ILS:2", "Press Here",
                        [{"branch": "Peoria PL - North", "call_number": "JP TUL",
                          "status": "Juvenile Board Book"}], "2026-07-20T00:00:00")
    s2 = db.record_scrape(conn, "detail", "2026-07-26T00:00:00", source="test")
    db.add_availability(conn, s2, "SD_ILS:2", "Press Here",
                        [{"branch": "Peoria PL - North", "call_number": "JP TUL",
                          "status": "Checked Out"}], "2026-07-26T00:00:00")
    conn.commit()

    latest = db.latest_availability(conn)
    assert len(latest) == 1                       # newest row per (record, branch)
    assert latest[0]["state"] == "out"
    assert latest[0]["checked_at"] == "2026-07-26T00:00:00"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
