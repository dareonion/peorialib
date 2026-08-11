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


def test_stem_pairs_must_be_near_exact():
    # shared series prefix + dropped subtitle ≠ the same book
    cands = [{"title": "Chicka chicka you you : a mirror book.",
              "authors": ["Martin, Bill, 1916-2004."], "format_class": "board",
              "items": [{"state": "available"}]}]
    best, _ = ba.pick_best("Chicka Chicka I love you", "Martin, Bill, 1916-2004",
                           "picture", cands)
    assert best is None
    # …while a true stem match still passes
    cands = [{"title": "Dear zoo : a lift-the-flap book",
              "authors": ["Campbell, Rod, 1945-"], "format_class": "board",
              "items": []}]
    best, _ = ba.pick_best("Dear zoo", "Campbell, Rod, 1945- author", "board",
                           cands)
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
    # 'That's Not My Hat' must not satisfy a want for 'This Is Mine!' —
    # neither through its romanized title nor through its CJK multiscript title
    cands = [{"title": "Zhe bu shi wo de mao zi", "authors": [],
              "format_class": "picture"}]
    best, _ = ba.pick_best("這是我的！", "三浦太郎", None, cands)
    assert best is None
    cands = [{"title": "Zhe bu shi wo de mao zi", "alt_title": "這不是我的帽子",
              "authors": [], "format_class": "picture"}]
    best, _ = ba.pick_best("這是我的！", "三浦太郎", None, cands)
    assert best is None
    # native-pinyin want vs a sibling title from the same series
    cands = [{"title": "Xiao xiong de du qi", "authors": [], "format_class": "picture"}]
    best, _ = ba.pick_best("Xiao xiong de wei ba", None, None, cands)
    assert best is None
    # joined-syllable romanization ('Keke' = Corduroy) is still pinyin-ish
    cands = [{"title": "Xiao xiong Keke de kou daii", "authors": [],
              "format_class": "picture"}]
    best, _ = ba.pick_best("Xiao xiong de ha qian", None, None, cands)
    assert best is None
    # …while the genuinely same title still passes, traditional → romanized
    cands = [{"title": "Hao e de mao mao chong", "authors": [], "format_class": "book"}]
    best, score = ba.pick_best("好餓的毛毛蟲", None, None, cands)
    assert best is not None and score >= 0.9


def test_pinyin_uses_catalog_romanization_for_shei():
    assert ba._pinyin("誰的家到了") == "shui de jia dao le"


def test_language_pinned_wants():
    # 'Cher zoo' (lang=fre) must not take the English Dear Zoo…
    cands = [{"title": "Dear zoo", "authors": ["Campbell, Rod"],
              "format_class": "picture", "language": "eng"}]
    best, _ = ba.pick_best("Cher zoo", "Campbell, Rod", None, cands, "fre")
    assert best is None
    # …but does take the French edition
    cands.append({"title": "Cher zoo", "authors": ["Campbell, Rod"],
                  "format_class": "picture", "language": "fre"})
    best, _ = ba.pick_best("Cher zoo", "Campbell, Rod", None, cands, "fre")
    assert best is not None and best["language"] == "fre"
    # series sibling in the right language still isn't the same book (0.8 bar)
    cands = [{"title": "T'choupi visite Paris", "authors": ["Courtin, Thierry"],
              "format_class": "picture", "language": "fre"}]
    best, _ = ba.pick_best("T'choupi va sur le pot", "Courtin, Thierry", None,
                           cands, "fre")
    assert best is None
    # MVPL: language derived from 'J FRENCH …' call numbers
    items = [{"call_number": "J FRENCH J P TISON"}]
    assert ba._webpac_language(items) == "fre"
    assert ba._webpac_language([{"call_number": "J P SCHERTLE"}]) is None


def test_load_excludes():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ex.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write('{"_comment": "x", "titles": ["Xiao fang che"]}')
        assert ba.load_excludes(p) == {"Xiao fang che"}
        assert ba.load_excludes(os.path.join(d, "missing.json")) == set()


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
    assert ids == ["S118C542544", "S118C131874", "S118C14525", "S118C99001"]
    assert cands[0]["format_class"] == "board"
    assert cands[2]["format_class"] == "picture"
    assert cands[3]["format_class"] == "ebook"   # digital tracked, classed
    # movie/music formats never become candidates
    import copy
    dvd = copy.deepcopy(BC_SEARCH)
    dvd["entities"]["bibs"]["S118C99001"]["briefInfo"]["format"] = "DVD"
    assert [c["bib_id"] for c in ba.bc_parse_search(dvd)] == \
        ["S118C542544", "S118C131874", "S118C14525"]


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
    # no media icon, but every copy shelved under Movies/Music → still dropped
    shelf_page = WEBPAC_PAGE.replace(
        ' <img src="/screens/media_book.gif" alt="Children\'s Board Book"><br />',
        '').replace("Children's Board Books - 1st Floor",
                    "Children's Movies - 1st Floor", 2)  # both rows of entry 1 only
    cands = ba.webpac_parse_results(shelf_page)
    assert [c["bib_id"] for c in cands] == ["b1229472"]
    # single-hit record view for a DVD → no candidate either
    dvd_record = WEBPAC_RECORD_PAGE.replace(
        'class="bibInfoLabel">Author</td>',
        'class="bibInfoLabel">Material</td>').replace(
        '<span style="color:RED" ><strong>McMullan</strong></span>, Kate.',
        '1 videodisc (DVD) : sound, color')
    assert ba.webpac_parse_results(dvd_record) == []


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


def test_webpac_query_folds_accents_and_cjk():
    assert ba.webpac_query("Bébés chouettes Waddell") == "Bebes chouettes Waddell"
    assert ba.webpac_query("好餓的毛毛蟲") == "hao e de mao mao chong"
    # apostrophes join — a split leaves stray one-letter tokens ('t', 'T')
    # that AND a keyword search down to zero hits
    assert ba.webpac_query("Giraffes can't dance") == "Giraffes cant dance"
    assert ba.webpac_query("Mr. Gumpy's outing") == "Mr Gumpys outing"
    assert ba.webpac_query("T'choupi va sur le pot") == "Tchoupi va sur le pot"
    # mid-query 'not' is a boolean operator to WebPAC; leading 'not' is a term
    assert ba.webpac_query("But not the hippopotamus") == "But the hippopotamus"
    assert ba.webpac_query("Not a box") == "Not a box"


def test_webpac_state_words():
    assert ba.webpac_state("AVAILABLE") == "available"
    assert ba.webpac_state("CHECK SHELF") == "available"
    assert ba.webpac_state("DUE 08-16-26") == "out"
    assert ba.webpac_state("ON HOLDSHELF") == "out"
    assert ba.webpac_state("LIB USE ONLY") == "reference"
    assert ba.webpac_state("UNAVAILABLE") == "out"     # substring trap (LINK+)
    assert ba.webpac_state("1 HOLD") == "out"


LINKPLUS_RESULTS = """
<tr><td class="briefcitCell"><div class="briefcitRow"><div class="briefcitLeft">
<div class="briefcitEntryNum"><a name='anchor_2'></a> 2</div>
<div class="briefcitMark"><input type="checkbox" name="save" value="b50144994" ></div>
</div><div class="briefcitJacket">&nbsp;</div><div class="briefcitDetail">
<div class="briefcitDetailMain"><h2 class="briefcitTitle">
<a href="/x">Dear zoo</a></h2><br >Campbell, Rod, 1945- author, illustrator.<br >
New York : Little Simon, 2019.<br />1 volume (unpaged) :&nbsp;</div></div></div></td></tr>
<tr><td class="briefcitCell"><div class="briefcitRow"><div class="briefcitLeft">
<div class="briefcitMark"><input type="checkbox" name="save" value="b45943117" ></div>
</div><div class="briefcitDetail"><div class="briefcitDetailMain">
<h2 class="briefcitTitle"><a href="/x">Dear zoo</a></h2><br >Campbell, Rod.<br >
[S.l.] : Weston Woods, 2005.<br />1 videodisc (10 min.) :&nbsp;</div></div></div></td></tr>
"""

LINKPLUS_HOLDINGS = """
<table width=100% class="centralDetailHoldings" align="center"><tr>
<th align="left">Library</th><th>Shelving Location</th><th>Electronic Link</th>
<th>Call Number and Holdings</th><th>Request Status</th></tr>
<tr class="holdings9alam"><td><a name="9alam"></a>Alameda County Public</td>
<td>Albany Childrens Picture Book</td><td>&nbsp; </td>
<td>JPB CAMPBELL,R </td><td>DUE 08-15-26</td><td></tr>
<tr class="holdings92pal"><td><a name="92pal"></a>Palo Alto Public Library</td>
<td>Mitchell Park - Children's - Picture Book</td><td>&nbsp; </td>
<td>J PICTURE BOOK CAMPBELL </td><td>AVAILABLE</td><td></tr>
<tr class="holdings9cruz"><td><a name="9cruz"></a>Santa Cruz Public Libraries</td>
<td>Aptos Children's</td><td>&nbsp; </td>
<td>JJ CAMPBELL </td><td>UNAVAILABLE</td><td></tr>
</table>
"""


def test_linkplus_parse_results_drops_videodisc():
    cands = ba.linkplus_parse_results(LINKPLUS_RESULTS)
    assert [c["bib_id"] for c in cands] == ["b50144994"]  # DVD edition dropped
    assert cands[0]["title"] == "Dear zoo"
    assert cands[0]["authors"] == ["Campbell, Rod, 1945- author, illustrator."]


def test_linkplus_parse_holdings():
    items = ba.linkplus_parse_holdings(LINKPLUS_HOLDINGS)
    assert len(items) == 3
    by_lib = {i["branch"]: i for i in items}
    assert by_lib["Palo Alto Public Library"]["state"] == "available"
    assert by_lib["Alameda County Public"]["state"] == "out"
    assert by_lib["Santa Cruz Public Libraries"]["state"] == "out"
    assert by_lib["Palo Alto Public Library"]["call_number"] == "J PICTURE BOOK CAMPBELL"


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
        # favorites view: Cupertino's copy is out -> ✗ in the Cupertino column
        assert "Your branches" in overview
        fav_row = [l for l in overview.splitlines()
                   if l.startswith("| Dear zoo") and "✗" in l]
        assert fav_row, "Cupertino column should show the out state"
        sccl = open(os.path.join(d, "sccl.md"), encoding="utf-8").read()
        assert "Milpitas Library — 1 on the shelf" in sccl
        assert "Not found in this catalog" in sccl

        # a corrected match supersedes the old scrape's whole footprint: the
        # old branches must vanish, not linger as "latest" for their shelves
        conn = db.open_db(dbp)
        ts2 = "2026-08-07T12:00:00"
        sid2 = db.record_scrape(conn, "remote", ts2, source="test", profile="sccl")
        db.upsert_remote_bib(conn, "sccl", "SD_ILS:1", ts2,
                             {"bib_id": "S118C99999", "title": "Dear Zoo",
                              "format": "BOARD_BK"}, 1.0)
        db.add_remote_availability(conn, sid2, "sccl", "SD_ILS:1", "S118C99999",
                                   "Dear zoo",
                                   [{"branch": "Saratoga Library",
                                     "call_number": "J TODDLER",
                                     "status": "In", "state": "available"}], ts2)
        conn.commit()
        rows = db.latest_remote_availability(conn)
        assert {r["branch"] for r in rows} == {"Saratoga Library"}
        conn.close()


def test_report_dedupes_and_discloses_odd_editions():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        conn = db.open_db(dbp)
        ts = "2026-08-10T10:00:00"
        title = "The very hungry caterpillar"
        # two want rows (duplicate Peoria editions of the same book)
        db.upsert_title(conn, "SD_ILS:1", title, ts, {"format": "picture"})
        db.upsert_title(conn, "SD_ILS:2", title, ts, {"format": "board"})
        sid = db.record_scrape(conn, "remote", ts, source="test", profile="sccl")
        item = [{"branch": "Cupertino Library", "call_number": "J TODDLER",
                 "status": "Available", "state": "available"}]
        for rid, kind in (("SD_ILS:1", "primary"), ("SD_ILS:2", "edition")):
            db.upsert_remote_bib(conn, "sccl", rid, ts,
                                 {"bib_id": "b1", "title": title}, 1.0)
            db.replace_remote_editions(conn, "sccl", rid, [
                {"bib_id": "b1", "title": title, "authors": [],
                 "format": "BOARD_BK", "format_class": "board",
                 "language": "eng", "kind": kind, "match_score": 1.0},
                # a second printing of the same work — renders identically
                {"bib_id": "b2", "title": title, "authors": [],
                 "format": "BOARD_BK", "format_class": "board",
                 "language": "eng", "kind": "edition", "match_score": 1.0},
                # an odd-titled 'edition' must disclose its real title
                {"bib_id": "b3", "title": "The Very Hungry Caterpillar's Eid",
                 "authors": [], "format": "BOARD_BK", "format_class": "board",
                 "language": "eng", "kind": "edition", "match_score": 0.95},
            ], ts)
            db.add_remote_availability(conn, sid, "sccl", rid, "b1", title,
                                       item, ts)
            db.add_remote_availability(conn, sid, "sccl", rid, "b2", title,
                                       item, ts)
            db.add_remote_availability(conn, sid, "sccl", rid, "b3", title,
                                       item, ts)
        conn.commit()
        conn.close()
        report.write_bayarea(dbp, d)
        md = open(os.path.join(d, "sccl.md"), encoding="utf-8").read()
        # 2 want rows x 2 identical printings collapse into ONE shelf line...
        cupertino = md.split("## Cupertino")[1].split("##")[0]
        plain = [l for l in cupertino.splitlines()
                 if l.startswith("- ") and "Eid" not in l]
        assert len(plain) == 1, plain
        # ...and the primary's bib supplies the link
        assert "/record/b1" in plain[0]
        # the odd-titled edition is one line too, disclosing its real title
        eid = [l for l in cupertino.splitlines() if "Eid" in l]
        assert len(eid) == 1
        assert "board book: “The Very Hungry Caterpillar's Eid”" in eid[0]


def test_write_bayarea_is_noop_without_remote_data():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        db.open_db(dbp).close()
        assert report.write_bayarea(dbp, d) == []


# --- record details: compilation contents / translation originals ---------------

BC_MARC_PAGE = """
<tr><td scope="row" class="marcTag"><strong>100</strong></td>
<td class="marcIndicator">1 </td>
<td class="marcTagData">$aMartin, Bill,$d1916-2004$</td></tr>
<tr><td scope="row" class="marcTag"><strong>240</strong></td>
<td class="marcIndicator">10</td>
<td class="marcTagData">$aPolar bear, polar bear, what do you hear?$lSpanish$</td></tr>
<tr><td scope="row" class="marcTag"><strong>245</strong></td>
<td class="marcIndicator">10</td>
<td class="marcTagData">$aOso polar, oso polar, que es ese ruido? /$cpor Bill Martin$</td></tr>
<tr><td scope="row" class="marcTag"><strong>505</strong></td>
<td class="marcIndicator">0 </td>
<td class="marcTagData">$aBrown bear, brown bear, what do you see? --
Polar bear, polar bear, what do you hear? -- Panda bear, panda bear, what do
you see?$</td></tr>
"""

MVPL_DETAIL_PAGE = """
<tr><td width="20%" class="bibInfoLabel">Title</td>
<td class="bibInfoData">Brown bear &amp; friends</td></tr>
<tr><td width="20%" class="bibInfoLabel">Note</td>
<td class="bibInfoData">Read by Gwyneth Paltrow.</td></tr>
<tr><td width="20%" class="bibInfoLabel">Note</td>
<td class="bibInfoData">Translation of: Polar bear, polar bear, what do you
hear?</td></tr>
<tr><td width="20%" class="bibInfoLabel">Contents</td>
<td class="bibInfoData">Brown bear, brown bear, what do you see? --
Baby bear, baby bear, what do you see?</td></tr>
"""


def test_bc_marc_details():
    d = ba.bc_details_from_page(BC_MARC_PAGE)
    assert d["orig_title"] == "Polar bear, polar bear, what do you hear?"
    assert d["contents"].startswith("Brown bear, brown bear, what do you see?; "
                                    "Polar bear")
    assert "$" not in d["contents"]


def test_mvpl_record_details():
    d = ba.mvpl_details_from_page(MVPL_DETAIL_PAGE)
    # the translation note is the SECOND Note row — must still be found
    assert d["orig_title"] == "Polar bear, polar bear, what do you hear?"
    assert d["contents"] == ("Brown bear, brown bear, what do you see?; "
                             "Baby bear, baby bear, what do you see?")


# --- multi-edition tracking (pick_all + remote_editions) ------------------------

def _cand(bib, title, fmt_class, lang=None, authors=("Martin, Bill",),
          strict=True, fmt=None):
    return {"bib_id": bib, "strict": strict, "title": title, "subtitle": None,
            "alt_title": None, "authors": list(authors),
            "format": fmt or fmt_class, "format_class": fmt_class,
            "year": None, "language": lang}


POLAR_WANT = ("Polar bear, polar bear, what do you hear?", "Martin, Bill",
              "picture")
POLAR_CANDS = [
    _cand("b1", "Polar Bear, Polar Bear, What Do You Hear?", "picture", "eng"),
    _cand("b2", "Polar Bear, Polar Bear, What Do You Hear?", "board", "eng"),
    _cand("b3", "Oso polar, oso polar, qué es ese ruido?", "picture", "spa"),
    _cand("b4", "Brown bear & friends", "audio", "eng"),  # compilation audiobook
    _cand("b5", "Panda Bear, Panda Bear, What Do You See?", "picture", "eng"),
]


def test_pick_all_collects_editions_translations_audio():
    best, score, eds = ba.pick_all(*POLAR_WANT, POLAR_CANDS)
    assert best["bib_id"] == "b1" and score > 0.9
    kinds = {e["bib_id"]: e["kind"] for e in eds}
    # board edition, Spanish translation, audio compilation — but NOT the
    # series sibling: Panda Bear scores 0.773, above MATCH_THRESHOLD yet
    # below EDITION_MIN_RATIO
    assert kinds == {"b1": "primary", "b2": "edition",
                     "b3": "translation", "b4": "audio"}


def test_pick_all_loose_rules_need_strict_source():
    # smart-search (non-strict) results can be the author's unrelated works,
    # so the author-anchored rules must ignore them
    cands = [dict(c, strict=False) for c in POLAR_CANDS]
    _, _, eds = ba.pick_all(*POLAR_WANT, cands)
    assert {e["kind"] for e in eds} == {"primary", "edition"}


def test_pick_all_rejects_suffix_spinoffs():
    # short-suffix spinoffs land just under 0.95 — 'Eid' scored 0.900 and
    # 'Dragons Love Tacos 2' 0.947, and both sailed under the want's name
    cands = [
        _cand("v1", "The Very Hungry Caterpillar", "picture", "eng",
              authors=("Carle, Eric",)),
        _cand("v2", "The Very Hungry Caterpillar's Eid", "board", "eng",
              authors=("Carle, Eric",)),
    ]
    _, _, eds = ba.pick_all("The very hungry caterpillar", "Carle, Eric",
                            "picture", cands)
    assert [e["bib_id"] for e in eds] == ["v1"]
    cands = [
        _cand("d1", "Dragons Love Tacos", "picture", "eng",
              authors=("Rubin, Adam",)),
        _cand("d2", "Dragons Love Tacos 2", "picture", "eng",
              authors=("Rubin, Adam",)),
    ]
    _, _, eds = ba.pick_all("Dragons love tacos", "Rubin, Adam",
                            "picture", cands)
    assert [e["bib_id"] for e in eds] == ["d1"]


def test_borderline_translation_reenters_via_translation_rule():
    # 'Pete el gato' scores ~0.91 on the shared subtitle — too low for an
    # edition now, but the translation rule takes it, correctly labeled
    cands = [
        _cand("p1", "Pete the cat : I love my white shoes", "picture", "eng",
              authors=("Litwin, Eric",)),
        _cand("p2", "Pete el gato : I love my white shoes", "picture", "spa",
              authors=("Litwin, Eric",)),
    ]
    _, _, eds = ba.pick_all("Pete the cat : I love my white shoes",
                            "Litwin, Eric", "picture", cands)
    kinds = {e["bib_id"]: e["kind"] for e in eds}
    assert kinds == {"p1": "primary", "p2": "translation"}


def test_pick_all_keeps_closest_translation_per_language():
    # 'Translation of: <spinoff>' notes contain the original title's every
    # word, so a spinoff translation reaches the strict result set too; the
    # length-closest title per (language, format) wins
    cands = POLAR_CANDS + [
        _cand("b6", "Oso polar de Pascua, los colores del gran oso polar",
              "picture", "spa")]
    _, _, eds = ba.pick_all(*POLAR_WANT, cands)
    assert [e["bib_id"] for e in eds if e["kind"] == "translation"] == ["b3"]


def test_pick_all_pinned_lang_stays_narrow():
    # a French want must not drag the English record along as an 'edition'
    cands = [
        _cand("f1", "Cher zoo", "picture", "fre", authors=("Campbell, Rod",)),
        _cand("f2", "Dear zoo", "picture", "eng", authors=("Campbell, Rod",)),
    ]
    best, _, eds = ba.pick_all("Cher zoo", "Campbell, Rod", "picture", cands,
                               lang="fre")
    assert best["bib_id"] == "f1"
    assert [e["bib_id"] for e in eds] == ["f1"]


def test_audiobook_media_kept_and_classed():
    items = [{"branch": "Children's Audiobooks", "collection": None,
              "call_number": "J CD MARTIN", "status": "AVAILABLE",
              "state": "available"}]
    assert not ba._webpac_nonbook("Audiobook", items)
    assert ba._webpac_fmt_class("Audiobook", items) == "audio"
    assert ba._webpac_nonbook("DVD", items)            # movies still out
    music = [dict(items[0], branch="Music - 2nd floor")]
    assert ba._webpac_nonbook("Audiobook", music)      # music shelf still out
    assert ba._bc_format_class("PLAYAWAY_AUDIOBOOK") == "audio"
    assert "PLAYAWAY_AUDIOBOOK" in ba.BC_BOOK_FORMATS


def test_digital_editions_kept_and_classed():
    assert ba._bc_format_class("EBOOK") == "ebook"
    assert ba._bc_format_class("EAUDIOBOOK") == "eaudio"
    assert {"EBOOK", "EAUDIOBOOK"} <= ba.BC_BOOK_FORMATS
    assert not ba._webpac_nonbook("eBook", [])
    assert ba._webpac_fmt_class("eBook", []) == "ebook"
    assert ba._webpac_fmt_class("Downloadable Audiobook", []) == "eaudio"
    # same-title eBook joins as an edition; an eAudio compilation needs the
    # author anchor, exactly like physical audio
    cands = POLAR_CANDS + [
        _cand("b7", "Polar Bear, Polar Bear, What Do You Hear?", "ebook", "eng"),
        _cand("b8", "Bill Martin's bear stories", "eaudio", "eng"),
    ]
    _, _, eds = ba.pick_all(*POLAR_WANT, cands)
    kinds = {e["bib_id"]: e["kind"] for e in eds}
    assert kinds["b7"] == "edition" and kinds["b8"] == "audio"


def test_editions_store_report_labels_links_and_supersede():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        conn = db.open_db(dbp)
        ts = "2026-08-08T10:00:00"
        title = "Polar bear, polar bear, what do you hear?"
        db.upsert_title(conn, "SD_ILS:1", title, ts, {"format": "picture"})
        sid = db.record_scrape(conn, "remote", ts, source="test", profile="mvpl")
        eds = [
            {"bib_id": "b1", "title": title, "authors": ["Martin, Bill"],
             "format": "Children's Picture Book", "format_class": "picture",
             "language": None, "kind": "primary", "match_score": 1.0},
            {"bib_id": "b3", "title": "Oso polar, oso polar, ¿qué es ese ruido?",
             "authors": ["Martin, Bill"], "format": "Children's World Language",
             "format_class": "board", "language": "spa", "kind": "translation",
             "match_score": 0.3, "orig_title": title},
            {"bib_id": "b4", "title": "Brown bear & friends",
             "authors": ["Martin, Bill"], "format": "Audiobook",
             "format_class": "audio", "language": None, "kind": "audio",
             "match_score": 0.5,
             "contents": "Brown bear, brown bear, what do you see?; "
                         "Polar bear, polar bear, what do you hear?"},
            {"bib_id": "b7", "title": title, "authors": ["Martin, Bill"],
             "format": "eBook", "format_class": "ebook", "language": "eng",
             "kind": "edition", "match_score": 1.0},
        ]
        db.upsert_remote_bib(conn, "mvpl", "SD_ILS:1", ts,
                             {"bib_id": "b1", "title": title}, 1.0)
        db.replace_remote_editions(conn, "mvpl", "SD_ILS:1", eds, ts)
        db.add_remote_availability(conn, sid, "mvpl", "SD_ILS:1", "b1", title,
                                   [{"branch": "Children's Picture Books",
                                     "call_number": "J P MARTIN",
                                     "status": "DUE 09-01-26", "state": "out"}],
                                   ts)
        db.add_remote_availability(conn, sid, "mvpl", "SD_ILS:1", "b3", title,
                                   [{"branch": "Children's World Language",
                                     "call_number": "J SPANISH J BOARD M",
                                     "status": "AVAILABLE",
                                     "state": "available"}], ts)
        conn.commit()
        # edition rows pass the current-bib join alongside the primary's
        rows = db.latest_remote_availability(conn)
        assert {r["bib_id"] for r in rows} == {"b1", "b3"}
        conn.close()

        report.write_bayarea(dbp, d)
        mv = open(os.path.join(d, "mountainview.md"), encoding="utf-8").read()
        # the Spanish board book is on the shelf: labeled with its real
        # (spine) title, linked
        assert ("— Spanish board book: “Oso polar, oso polar, ¿qué es ese "
                "ruido?”" in mv)
        assert "https://classiccatalog.mountainview.gov/record=b3" in mv
        # the plain edition is checked out → linked in the 'no copy' section
        assert (f"[{title}](https://classiccatalog.mountainview.gov/record=b1)"
                in mv)
        # the audio compilation is a different work — its own title AND what
        # it contains must show
        assert "audiobook: “Brown bear & friends”" in mv
        assert ("(contains: Brown bear, brown bear, what do you see?; "
                "Polar bear, polar bear, what do you hear?)" in mv)
        # a translation names its original
        assert f"— translation of “{title}”" in mv
        # the eBook lands in the Digital section, not the shelf lists
        assert "## Digital" in mv and "(eBook)" in mv

        # a re-lookup that drops the translation supersedes its availability
        conn = db.open_db(dbp)
        db.replace_remote_editions(conn, "mvpl", "SD_ILS:1", eds[:1],
                                   "2026-08-08T11:00:00")
        conn.commit()
        rows = db.latest_remote_availability(conn)
        assert {r["bib_id"] for r in rows} == {"b1"}
        conn.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
