"""Tests for report.py (markdown generation) — seeds a temp DB, no browser."""
from __future__ import annotations

import os
import tempfile

import catalog_db as db
import report


def _seed(db_path):
    conn = db.open_db(db_path)
    ts = "2026-07-26T12:00:00"
    db.upsert_title(conn, "SD_ILS:1", "Yue liang wan an (Goodnight Moon, Chinese)", ts)
    db.upsert_title(conn, "SD_ILS:2", "Kitten's first full moon", ts)
    s = db.record_scrape(conn, "detail", ts, source="test")
    db.add_availability(conn, s, "SD_ILS:1", "Yue liang wan an (Goodnight Moon, Chinese)",
                        [{"branch": "North", "call_number": "JP BRO",
                          "status": "Juvenile World Language Collection"}], ts)
    # two copies at North: one out, one available -> should collapse to one "on shelf"
    db.add_availability(conn, s, "SD_ILS:2", "Kitten's first full moon",
                        [{"branch": "North", "call_number": "JP HEN", "status": "Checked Out"},
                         {"branch": "North", "call_number": "JP HEN", "status": "Juvenile Picture Book"}], ts)
    conn.commit()
    conn.close()


def test_write_all_creates_files_from_db():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        _seed(dbp)
        written = report.write_all(dbp, outdir=d)
        names = {os.path.basename(p) for p in written}
        assert names == {"books.md", "north.md", "lakeview.md", "main.md"}

        north = open(os.path.join(d, "north.md"), encoding="utf-8").read()
        # Chinese title routed to the World Language section
        assert "## Chinese / World Language" in north
        assert "Yue liang wan an" in north
        # multi-copy title appears exactly once, and counts as on-shelf (one copy available)
        assert north.count("Kitten's first full moon") == 1
        assert "**2 titles on the shelf**" in north

        books = open(os.path.join(d, "books.md"), encoding="utf-8").read()
        assert "AUTO-GENERATED" in books
        assert "Full availability matrix" in books
        assert "| North |" in books


def test_peoria_records_are_linked():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        _seed(dbp)
        report.write_all(dbp, outdir=d)
        books = open(os.path.join(d, "books.md"), encoding="utf-8").read()
        assert ("detailnonmodal/ent:$002f$002fSD_ILS$002f0$002fSD_ILS:2/one"
                in books)
        north = open(os.path.join(d, "north.md"), encoding="utf-8").read()
        assert "[Kitten's first full moon](https://alsi.sdp.sirsi.net" in north


def test_lakeview_empty_is_graceful():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        _seed(dbp)  # nothing at Lakeview
        report.write_all(dbp, outdir=d)
        lake = open(os.path.join(d, "lakeview.md"), encoding="utf-8").read()
        assert "(none right now)" in lake


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
