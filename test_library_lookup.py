"""Tests for library_lookup.

The pure helpers are tested directly. The two page-side extractors (SEARCH_JS,
DETAIL_JS) are validated against faithful HTML fixtures loaded locally via
set_content — no network, so the Cloudflare-guarded live site is never touched.

Run:  uv run pytest -q      (or: uv run python test_library_lookup.py)
"""
from __future__ import annotations

import library_lookup as L


# --------------------------- pure helpers ---------------------------

def test_classify():
    assert L.classify("Juvenile Board Book") == "available"
    assert L.classify("Juvenile Picture Book") == "available"
    assert L.classify("Checked Out") == "out"
    assert L.classify("Being transferred between libraries") == "out"
    assert L.classify("Children's Workroom") == "reference"
    assert L.classify("Non-Circulating Book") == "reference"


def test_status_label():
    assert L.status_label("Juvenile Board Book") == "✓ on shelf"
    assert L.status_label("Checked Out") == "✗ checked out"
    assert L.status_label("Being transferred between libraries") == "→ in transit"
    assert L.status_label("Children's Workroom") == "· in-library use only"


def test_summarize_avail():
    assert L.summarize_avail("3") == (3, "3 copies available now (Peoria PL)")
    assert L.summarize_avail("1") == (1, "1 copy available now (Peoria PL)")
    n, txt = L.summarize_avail("No copies available at Peoria PL, 12 copies available at other libraries")
    assert n == 0 and "12 at other RSA libraries" in txt
    assert L.summarize_avail("") == (None, "availability unknown")


def test_detail_url():
    assert L.detail_url("SD_ILS:1244737").endswith(
        "detailnonmodal/ent:$002f$002fSD_ILS$002f0$002fSD_ILS:1244737/one"
    )


def test_filter_branches():
    rows = [{"branch": "North"}, {"branch": "Lakeview"}, {"branch": "Lincoln"}]
    assert L.filter_branches(rows, []) == rows
    assert [r["branch"] for r in L.filter_branches(rows, ["north"])] == ["North"]
    assert {r["branch"] for r in L.filter_branches(rows, ["north", "lakeview"])} == {"North", "Lakeview"}


# --------------------------- extraction JS against fixtures ---------------------------

# Mirrors the real results page: .results_cell blocks with a .results_bio link,
# labelled text lines, an SD_ILS id in the markup, and an .availableNumber span.
# One row is an eResource with no SD_ILS (must be dropped by the id filter).
RESULTS_HTML = """
<div class="results_cell">
  <div class="results_bio"><a href="/…/ent:%2f%2fSD_ILS%2f0%2fSD_ILS:1244737/one">Little blue truck</a></div>
  <div>Author Schertle, Alice author</div>
  <div>Call Number: JP SCH</div>
  <div>Publication Date: 2008</div>
  <span class="availableNumber">1</span>
</div>
<div class="results_cell">
  <div class="results_bio"><a href="/…/ent:%2f%2fSD_ILS%2f0%2fSD_ILS:2306658/one">Moo, baa, la la la!</a></div>
  <div>Author Boynton, Sandra</div>
  <div>Call Number: JP BOY</div>
  <div>Publication Date: 2019</div>
  <span class="availableNumber">No copies available at Peoria PL, 7 copies available at other libraries</span>
</div>
<div class="results_cell">
  <div class="results_bio"><a href="/eresource">Dear Zoo (ebook)</a></div>
  <div>Call Number:</div>
  <span class="availableNumber">Unlimited</span>
</div>
"""

# Mirrors the holdings table: header row, doubled library-name cells (mobile+desktop
# layers), and status cells that end in a stray "Unknown".
DETAIL_HTML = """
<table>
  <tr><th>Library</th><th>Call Number</th><th>Material Type</th><th>Reading Level</th><th>Status</th></tr>
  <tr>
    <td><span>Peoria PL - North</span><span>Peoria PL - North</span></td>
    <td>JP SCH</td><td>Book</td><td>Juvenile</td>
    <td><span>Juvenile Board Book</span><span>Unknown</span></td>
  </tr>
  <tr>
    <td><span>Peoria PL - Lincoln</span><span>Peoria PL - Lincoln</span></td>
    <td>JP SCH</td><td>Book</td><td>Juvenile</td>
    <td><span>Checked Out</span><span>Unknown</span></td>
  </tr>
  <tr>
    <td><span>Peoria PL - Main St</span><span>Peoria PL - Main St</span></td>
    <td>JP SCH</td><td>Non-Circulating Book</td><td>Juvenile</td>
    <td><span>Children's Workroom</span><span>Unknown</span></td>
  </tr>
  <tr>
    <td>Alpha Park PLD</td><td>E SCH</td><td>Book</td><td>Juvenile</td><td>Checked Out</td>
  </tr>
</table>
"""


def _eval_fixture(html, js):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        pg = b.new_page()
        pg.set_content(f"<!doctype html><body>{html}</body>")
        out = pg.evaluate(js)
        b.close()
        return out


def test_search_js_extraction():
    hits = _eval_fixture(RESULTS_HTML, L.SEARCH_JS)
    kept = [h for h in hits if h.get("id")]  # same id filter lookup() applies
    assert len(kept) == 2, "eResource row without SD_ILS should drop out"

    first = kept[0]
    assert first["id"] == "SD_ILS:1244737"
    assert first["title"] == "Little blue truck"
    assert first["author"] == "Schertle, Alice author"
    assert first["call"] == "JP SCH"
    assert first["year"] == "2008"
    assert L.summarize_avail(first["avail"]) == (1, "1 copy available now (Peoria PL)")

    second_n, _ = L.summarize_avail(kept[1]["avail"])
    assert second_n == 0


def test_detail_js_extraction():
    rows = _eval_fixture(DETAIL_HTML, L.DETAIL_JS)
    # header + the non-Peoria row are excluded; three Peoria rows remain
    assert [r["branch"] for r in rows] == ["North", "Lincoln", "Main St"]
    assert rows[0]["status"] == "Juvenile Board Book"   # doubling + "Unknown" stripped
    assert rows[1]["status"] == "Checked Out"
    assert L.classify(rows[0]["status"]) == "available"
    assert L.classify(rows[1]["status"]) == "out"
    assert L.classify(rows[2]["status"]) == "reference"

    north = L.filter_branches(rows, ["north"])
    assert len(north) == 1 and north[0]["branch"] == "North"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
