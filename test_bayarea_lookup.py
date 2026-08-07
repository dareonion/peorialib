"""Tests for bayarea_lookup.py — parsing + matching against fixtures, no network."""
from __future__ import annotations

import os
import tempfile

import bayarea_lookup as ba
import catalog_db as db
import report

# --- fixtures (trimmed from real responses, 2026-08) ----------------------------

BC_SEARCH = {
    "catalogSearch": {"results": [
        {"representative": "S118C542544", "manifestations": ["S118C542544"]},
        {"representative": "S118C131874",
         "manifestations": ["S118C131874", "S118C14525", "S118C99001"]},
    ]},
    "entities": {"bibs": {
        "S118C542544": {"id": "S118C542544", "briefInfo": {
            "title": "Dear Zoo Animal Shapes", "subtitle": "",
            "authors": ["Campbell, Rod"], "format": "BOARD_BK",
            "publicationDate": "2016", "callNumber": "J TODDLER"}},
        "S118C131874": {"id": "S118C131874", "briefInfo": {
            "title": "Dear Zoo", "subtitle": "",
            "authors": ["Campbell, Rod"], "format": "BOARD_BK",
            "publicationDate": "1999", "callNumber": "J TODDLER"}},
        "S118C14525": {"id": "S118C14525", "briefInfo": {
            "title": "Dear Zoo", "subtitle": "",
            "authors": ["Campbell, Rod"], "format": "PICTURE_BOOK",
            "publicationDate": "1982", "callNumber": "J PICT BK"}},
        "S118C99001": {"id": "S118C99001", "briefInfo": {
            "title": "Dear Zoo", "subtitle": "",
            "authors": ["Campbell, Rod"], "format": "EBOOK",
            "publicationDate": "2011", "callNumber": None}},
    }},
}

BC_AVAILABILITY = {
    "entities": {"bibItems": {
        "1": {"collection": "Toddler Section", "callNumber": "J TODDLER",
              "branchName": "Cupertino Library",
              "availability": {"statusType": "UNAVAILABLE", "libraryUseOnly": False,
                               "libraryStatus": "Unavailable"}},
        "2": {"collection": "Toddler Section", "callNumber": "J TODDLER",
              "branchName": "Milpitas Library",
              "availability": {"statusType": "AVAILABLE", "libraryUseOnly": False,
                               "libraryStatus": "Available"}},
        "3": {"collection": "Reference", "callNumber": "J TODDLER",
              "branchName": "Gilroy Library",
              "availability": {"statusType": "AVAILABLE", "libraryUseOnly": True,
                               "libraryStatus": "In Library Use Only"}},
    }},
}

WEBPAC_PAGE = """
<tr><td class="briefCitRow"><table><tr valign="top">
<td><div class="briefcitEntryNum"><a name='anchor_1'></a> 1</div></td>
<td><div class="briefcitRequest"><input type="checkbox" name="save" value="b2712165" ></div></td>
<td class="briefcitDetail"><span class="briefcitTitle">
<a href="/x">Dear zoo : a lift-the-flap book</a></span>
<br />Campbell, Rod, 1945-<br />New York : Little Simon, 2007.<br /></td>
<td class="briefcitDetail">2007<br /> <img src="/screens/media_book.gif" alt="Children's Board Book"><br /></td>
</tr><tr><td colspan="2"><div class="briefcitItems">
<table class="bibItems">
<tr  class="bibItemsHeader"><th>LOCATION</th><th>CALL #</th><th>STATUS</th></tr>
<tr  class="bibItemsEntry">
<td><!-- field 1 -->&nbsp;Children's Board Books - 1st Floor </td>
<td><!-- field C -->&nbsp;<a href="/y">J BOARD C</a> <!-- field v -->&nbsp;</td>
<td><!-- field % -->&nbsp;DUE 08-16-26 </td></tr>
<tr  class="bibItemsEntry">
<td><!-- field 1 -->&nbsp;Children's Board Books - 1st Floor </td>
<td><!-- field C -->&nbsp;<a href="/y">J BOARD C</a> &nbsp;</td>
<td><!-- field % -->&nbsp;AVAILABLE </td></tr>
</table></div></td></tr></table></td></tr>
<tr><td class="briefCitRow"><table><tr valign="top">
<td><div class="briefcitEntryNum"><a name='anchor_2'></a> 2</div></td>
<td><div class="briefcitRequest"><input type="checkbox" name="save" value="b1229472" ></div></td>
<td class="briefcitDetail"><span class="briefcitTitle">
<a href="/x">Look after us : a lift-the-flap book</a></span>
<br />Campbell, Rod, 1945- author, illustrator.<br />New York : Little Simon, 2022.<br /></td>
<td class="briefcitDetail">2022<br /></td>
</tr><tr><td colspan="2"><div class="briefcitItems">
<table class="bibItems">
<tr  class="bibItemsEntry">
<td>&nbsp;Children's Board Books - 1st Floor </td>
<td>&nbsp;J BOARD REAL WORLD &nbsp;</td>
<td>&nbsp;AVAILABLE </td></tr>
</table></div></td></tr></table></td></tr>
"""


# --- query building -------------------------------------------------------------

def test_query_terms_strips_glosses_and_parallel_titles():
    assert ba.query_terms("Dong wu yue dui (Animal Band)") == ("Dong wu yue dui", "")
    assert ba.query_terms("Freight train = Tren de carga", "Crews, Donald") == \
        ("Freight train", "Crews")
    assert ba.query_terms("Long-haired girl (bilingual EN/ZH + audio)") == \
        ("Long-haired girl", "")
    assert ba.query_terms("Bluey : the creek.") == ("Bluey : the creek", "")
    assert ba.query_terms("Are you my mother?",
                          "Eastman, P. D. (Philip D.) author illustrator") == \
        ("Are you my mother?", "Eastman")


def test_series_subtitles_do_not_cross_match():
    cands = [{"title": "Grumpy monkey : Valentine gross-out",
              "authors": ["Lang, Suzanne"], "format_class": "picture"}]
    best, score = ba.pick_best("Grumpy monkey : mom for a day", "Lang, Suzanne",
                               "picture", cands)
    assert best is None  # different adventure, must not match on the shared stem
    # …but the bare series title may still take a subtitled edition
    best, _ = ba.pick_best("Grumpy monkey", "Lang, Suzanne", "picture", cands)
    assert best is not None


def test_exact_full_title_beats_stem_tie():
    cands = [{"title": "Grumpy monkey : Valentine gross-out",
              "authors": ["Lang, Suzanne"], "format_class": "picture",
              "items": [{"state": "available"}]},
             {"title": "Grumpy monkey",
              "authors": ["Lang, Suzanne"], "format_class": "picture",
              "items": []}]
    best, _ = ba.pick_best("Grumpy monkey", "Lang, Suzanne", "picture", cands)
    assert best["title"] == "Grumpy monkey"  # despite the other having a copy in


def test_cjk_want_matches_romanized_and_simplified_records():
    # SCCLD-style record: romanized main title, simplified-CJK multiscript
    cands = [{"title": "Hao e de mao mao chong", "subtitle": "",
              "alt_title": "好饿的毛毛虫", "authors": ["Carle, Eric"],
              "format_class": "picture"}]
    best, score = ba.pick_best("好餓的毛毛蟲", "Carle, Eric", None, cands)
    assert best is not None and score > 0.9
    # and an unrelated Chinese record stays unmatched
    decoy = [{"title": "Jing quan Weili", "alt_title": "警犬威利",
              "authors": [], "format_class": "book"}]
    best, score = ba.pick_best("好餓的毛毛蟲", None, None, decoy)
    assert best is None


def test_pinyin_lookalikes_are_rejected():
    # 'That's Not My Hat' must not satisfy a want for 'This Is Mine!'
    cands = [{"title": "Zhe bu shi wo de mao zi", "authors": [],
              "format_class": "picture"}]
    best, _ = ba.pick_best("這是我的！", "三浦太郎", None, cands)
    assert best is None
    # native-pinyin want vs a sibling title from the same series
    cands = [{"title": "Xiao xiong de du qi", "authors": [], "format_class": "picture"}]
    best, _ = ba.pick_best("Xiao xiong de wei ba", None, None, cands)
    assert best is None
    # …while the genuinely same title still passes, traditional → romanized
    cands = [{"title": "Hao e de mao mao chong", "authors": [], "format_class": "book"}]
    best, score = ba.pick_best("好餓的毛毛蟲", None, None, cands)
    assert best is not None and score >= 0.9


def test_pinyin_uses_catalog_romanization_for_shei():
    assert ba._pinyin("誰的家到了") == "shui de jia dao le"


def test_sync_wantlist_inserts_want_rows():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        wl = os.path.join(d, "wl.json")
        with open(wl, "w", encoding="utf-8") as fh:
            fh.write('[{"title": "好餓的毛毛蟲", "author": "Carle, Eric"}]')
        conn = db.open_db(dbp)
        assert ba.sync_wantlist(conn, wl) == 1
        row = conn.execute("SELECT * FROM titles").fetchone()
        assert row["record_id"] == "WANT:好餓的毛毛蟲"
        assert row["author"] == "Carle, Eric"
        ba.sync_wantlist(conn, wl)  # idempotent
        assert conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0] == 1
        conn.close()


# --- BiblioCommons parsing + matching -------------------------------------------

def test_bc_parse_search_orders_and_filters_formats():
    cands = ba.bc_parse_search(BC_SEARCH)
    ids = [c["bib_id"] for c in cands]
    assert ids == ["S118C542544", "S118C131874", "S118C14525"]  # EBOOK dropped
    assert cands[0]["format_class"] == "board"
    assert cands[2]["format_class"] == "picture"


def test_pick_best_prefers_exact_title_over_lookalike():
    cands = ba.bc_parse_search(BC_SEARCH)
    best, score = ba.pick_best("Dear zoo", "Campbell, Rod, 1945- author",
                               "picture", cands)
    assert best["bib_id"] == "S118C14525"  # picture-book Dear Zoo, not Animal Shapes
    assert score > 0.9


def test_pick_best_board_bonus_switches_edition():
    cands = ba.bc_parse_search(BC_SEARCH)
    best, _ = ba.pick_best("Dear zoo", "Campbell, Rod", "board", cands)
    assert best["format"] == "BOARD_BK"


def test_pick_best_rejects_junk():
    cands = ba.bc_parse_search(BC_SEARCH)
    best, score = ba.pick_best("Goodnight moon", "Brown, Margaret Wise", "picture",
                               cands)
    assert best is None and score < ba.MATCH_THRESHOLD


def test_bc_availability_states():
    items = ba.bc_parse_availability(BC_AVAILABILITY)
    by_branch = {i["branch"]: i for i in items}
    assert by_branch["Milpitas Library"]["state"] == "available"
    assert by_branch["Cupertino Library"]["state"] == "out"
    assert by_branch["Gilroy Library"]["state"] == "reference"


# --- WebPAC parsing -------------------------------------------------------------

def test_webpac_parse_results():
    cands = ba.webpac_parse_results(WEBPAC_PAGE)
    assert [c["bib_id"] for c in cands] == ["b2712165", "b1229472"]
    dz = cands[0]
    assert dz["title"] == "Dear zoo : a lift-the-flap book"
    assert dz["authors"] == ["Campbell, Rod, 1945-"]
    assert dz["format_class"] == "board"
    assert len(dz["items"]) == 2
    assert dz["items"][0]["branch"] == "Children's Board Books - 1st Floor"
    assert dz["items"][0]["call_number"] == "J BOARD C"
    assert dz["items"][0]["state"] == "out"          # DUE 08-16-26
    assert dz["items"][1]["state"] == "available"    # AVAILABLE


def test_webpac_skips_non_book_media():
    dvd_page = WEBPAC_PAGE.replace(
        'src="/screens/media_book.gif" alt="Children\'s Board Book"',
        'src="/screens/media_dvd.gif" alt="DVD"')
    cands = ba.webpac_parse_results(dvd_page)
    assert [c["bib_id"] for c in cands] == ["b1229472"]  # DVD entry dropped


def test_webpac_match_end_to_end():
    cands = ba.webpac_parse_results(WEBPAC_PAGE)
    best, score = ba.pick_best("Dear zoo", "Campbell, Rod, 1945- author", "board",
                               cands)
    assert best["bib_id"] == "b2712165"


WEBPAC_RECORD_PAGE = """
<tr><td valign="top" width="20%"  class="bibInfoLabel">Author</td>
<td class="bibInfoData">
<a href="/x"><span style="color:RED" ><strong>McMullan</strong></span>, Kate.</a>
</td></tr>
<tr><td valign="top" width="20%"  class="bibInfoLabel">Title</td>
<td class="bibInfoData">
<strong><span style="color:RED" ><strong>I</strong></span> <span style="color:RED" ><strong>stink</strong></span>! / Kate &amp; Jim McMullan.</strong>
</td></tr>
<a href="/search~S1?/.b1251479/.b1251479/1,1,1,B/marc~b1251479">MARC Display</a>
<span class="bibItems">
<table class="bibItems">
<tr class="bibItemsHeader"><th>LOCATION</th><th>CALL #</th><th>STATUS</th></tr>
<tr class="bibItemsEntry">
<td width="33%" ><!-- field 1 -->&nbsp;Children's Concept Books - 1st Floor </td>
<td width="43%" ><!-- field C -->&nbsp;<a href="/y">J P MCMULLAN VEH</a>&nbsp;</td>
<td width="24%" ><!-- field % -->&nbsp;AVAILABLE </td></tr>
</table></span>
<a href="/record=b1251479">record</a>
"""


def test_webpac_single_hit_record_page():
    cands = ba.webpac_parse_results(WEBPAC_RECORD_PAGE)
    assert len(cands) == 1
    rec = cands[0]
    assert rec["title"] == "I stink!"
    assert rec["bib_id"] == "b1251479"
    assert rec["authors"] == ["McMullan, Kate."]
    assert rec["items"][0]["state"] == "available"
    best, score = ba.pick_best("I stink!", "McMullan, Kate", "picture", cands)
    assert best is rec and score > 0.9


def test_webpac_state_words():
    assert ba.webpac_state("AVAILABLE") == "available"
    assert ba.webpac_state("CHECK SHELF") == "available"
    assert ba.webpac_state("DUE 08-16-26") == "out"
    assert ba.webpac_state("ON HOLDSHELF") == "out"
    assert ba.webpac_state("LIB USE ONLY") == "reference"


# --- store + report round-trip --------------------------------------------------

def test_remote_store_and_bayarea_markdown():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        conn = db.open_db(dbp)
        ts = "2026-08-07T10:00:00"
        db.upsert_title(conn, "SD_ILS:1", "Dear zoo", ts, {"format": "picture"})
        db.upsert_title(conn, "SD_ILS:2", "Xiao xiong san bu", ts)
        sid = db.record_scrape(conn, "remote", ts, source="test", profile="sccl")
        db.upsert_remote_bib(conn, "sccl", "SD_ILS:1", ts,
                             {"bib_id": "S118C14525", "title": "Dear Zoo",
                              "format": "PICTURE_BOOK"}, 0.97)
        db.add_remote_availability(conn, sid, "sccl", "SD_ILS:1", "S118C14525",
                                   "Dear zoo",
                                   [{"branch": "Milpitas Library",
                                     "call_number": "J PICT BK",
                                     "status": "Available", "state": "available"},
                                    {"branch": "Cupertino Library",
                                     "call_number": "J PICT BK",
                                     "status": "Unavailable", "state": "out"}], ts)
        db.upsert_remote_bib(conn, "sccl", "SD_ILS:2", ts, None, 0.3)  # not found
        conn.commit()

        rows = db.latest_remote_availability(conn)
        assert {r["branch"] for r in rows} == {"Milpitas Library", "Cupertino Library"}
        conn.close()

        written = report.write_bayarea(dbp, d)
        names = {os.path.basename(p) for p in written}
        assert names == {"bayarea.md", "sccl.md"}
        overview = open(os.path.join(d, "bayarea.md"), encoding="utf-8").read()
        assert "Dear zoo" in overview and "✓ 1" in overview
        assert "Xiao xiong san bu" in overview and "—" in overview
        sccl = open(os.path.join(d, "sccl.md"), encoding="utf-8").read()
        assert "Milpitas Library — 1 on the shelf" in sccl
        assert "Not found in this catalog" in sccl


def test_write_bayarea_is_noop_without_remote_data():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        db.open_db(dbp).close()
        assert report.write_bayarea(dbp, d) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
