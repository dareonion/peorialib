#!/usr/bin/env python3
"""Look up the peorialib want-list at Bay Area library systems.

Takes every title already in `peorialib.db` (the Peoria want-list) and checks
whether — and where — each one is on the shelf at:

  sccl  Santa Clara County Library District   (BiblioCommons; gateway JSON API)
  sjpl  San José Public Library                (BiblioCommons; gateway JSON API)
  mvpl  Mountain View Public Library           (classic Innovative WebPAC, HTML)

Unlike Peoria's catalog there is no Cloudflare wall here, so this is plain HTTP —
no browser needed. Results land in the same SQLite store (`remote_bibs` +
`remote_availability` in catalog_db) and the Bay Area markdown is regenerated
after every run.

    uv run bayarea_lookup.py                          # all titles, all systems
    uv run bayarea_lookup.py --system sccl --limit 5  # quick spot check
    uv run bayarea_lookup.py --resume                 # only titles not yet looked up
    uv run bayarea_lookup.py --title "dear zoo"       # ad-hoc probe, prints only

Matching is fuzzy: we search title + author-surname, then score candidates by
normalized title similarity (works for the pinyin Chinese titles too, since
these catalogs index romanized fields). A title can legitimately not match —
that library just doesn't hold it — and that's recorded as bib_id NULL.
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import html as htmllib
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime

import catalog_db as db
import report

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
GATEWAY = "https://gateway.bibliocommons.com/v2/libraries"

# BiblioCommons formats we'll accept as "this book" (physical, readable).
# BOOK_PCD / BOOK_CD / KIT cover the bilingual book-plus-audio kits on the list.
BC_BOOK_FORMATS = {"BK", "BOARD_BK", "PICTURE_BOOK", "PAPERBACK", "LARGE_PRINT",
                   "BOOK_PCD", "BOOK_CD", "KIT"}

MATCH_THRESHOLD = 0.62


# --- HTTP -----------------------------------------------------------------------

def _get(url: str, accept: str = "application/json", tries: int = 3,
         timeout: float = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": accept})
    last_err = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except Exception as e:  # URLError, HTTPError, timeout
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {last_err}")


# --- query building / fuzzy matching --------------------------------------------

def query_terms(title: str, author: str = None) -> tuple[str, str]:
    """(cleaned title, author surname) to feed a catalog search box.

    Drops the parenthetical glosses our list uses ('(Animal Band)', '(bilingual
    EN/ZH + audio)') and parallel titles after '=' or '/'.
    """
    t = re.sub(r"\([^)]*\)", " ", title or "")
    t = re.split(r"[=/]", t)[0]
    t = re.sub(r"\s+", " ", t).strip(" .,:;")
    surname = ""
    if author:
        surname = author.split(",")[0].strip()
        surname = re.sub(r"[^A-Za-z' -]", "", surname).strip()
    return t, surname


_CJK = re.compile(r"[一-鿿]")


def _norm(s: str) -> str:
    """Fold case/diacritics/punctuation so 'Hervé' == 'herve', keep CJK."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9一-鿿]+", " ", s.lower())
    return s.strip()


# pypinyin's colloquial readings vs the ALA-LC romanization catalogs actually use
_ALA_LC = {"shei": "shui"}


def _pinyin(s: str) -> str:
    """CJK → space-separated pinyin ('好餓的毛毛蟲' → 'hao e de mao mao chong').

    This is how the CJK want-list meets these catalogs' romanized title fields,
    and it also bridges traditional/simplified variants (both → the same pinyin).
    """
    from pypinyin import lazy_pinyin
    return " ".join(_ALA_LC.get(p, p) for p in lazy_pinyin(_norm(s)))


# A pinyin-mediated comparison must clear a higher bar: short syllable streams
# ('zhe shi wo de' vs 'zhe bu shi wo de mao zi') look far more alike to a
# sequence matcher than distinct English titles do.
PINYIN_MIN_RATIO = 0.85

_PINYIN_SYL = re.compile(r"^(zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw])?[aeiouv]{1,3}(n|ng|r)?$")


def _looks_pinyin(norm_text: str) -> bool:
    """True for romanized-Chinese strings ('xiao xiong de wei ba') — the DB's
    Peoria want-list stores pinyin natively, so it never carries a CJK flag.
    Majority vote, not all(): joined syllables ('Keke', 'Suqi') are common in
    catalog romanization and shouldn't unmask the string as non-pinyin."""
    toks = norm_text.split()
    if len(toks) < 3:
        return False
    hits = sum(1 for t in toks if _PINYIN_SYL.match(t))
    return hits * 3 >= len(toks) * 2


def _forms(title: str) -> list[tuple[str, bool, bool, bool]]:
    """[(text, is_stem, has_subtitle, is_pinyin)] — title, stem, pinyin forms."""
    stem = re.split(r"[:=/：]", title)[0].strip()
    has_sub = bool(stem) and stem != title
    out = [(title, False, has_sub, False)]
    if has_sub:
        out.append((stem, True, has_sub, False))
    for text, is_stem, hs, _ in list(out):
        if _CJK.search(text):
            out.append((_pinyin(text), is_stem, hs, True))
    return out


def title_score(want_title: str, cand_titles) -> float:
    """Best normalized-similarity between any want form and any candidate form.

    Each side also offers its pre-subtitle stem ('Dear zoo : a lift-the-flap
    book' → 'Dear zoo'), but a stem may only pair with a string that has no
    subtitle of its own, and stem pairs are slightly dampened — otherwise every
    'Grumpy monkey : <adventure>' would tie at 1.0 with every other one.
    Pairs where either side went through pinyin only count above PINYIN_MIN_RATIO.
    """
    best = 0.0
    for w, w_stem, w_sub, w_pin in _forms(want_title):
        wn = _norm(w)
        if not wn:
            continue
        for c in cand_titles:
            for cv, c_stem, c_sub, c_pin in _forms(c or ""):
                if (w_stem and c_sub) or (c_stem and w_sub):
                    continue  # dropping a subtitle may not erase a mismatch
                cn = _norm(cv)
                if not cn:
                    continue
                r = difflib.SequenceMatcher(None, wn, cn).ratio()
                # Chinese comparisons — pinyin or CJK — must be near-exact:
                # short syllable/character strings blur ('zhe shi wo de' vs
                # 'zhe bu shi wo de mao zi' is That's Not My Hat, and one 不
                # flips the meaning)
                loose = (w_pin or c_pin
                         or (_looks_pinyin(wn) and _looks_pinyin(cn))
                         or (_CJK.search(wn) and _CJK.search(cn)))
                if loose and r < PINYIN_MIN_RATIO:
                    continue
                if w_stem or c_stem:
                    r *= 0.98  # an exact full-title match should win ties
                best = max(best, r)
    return best


def _fmt_bonus(our_format: str, cand_class: str) -> float:
    """Nudge toward the same shelf format (board vs picture) when we know ours."""
    if our_format == "board" and cand_class == "board":
        return 0.08
    if our_format in ("picture", "reader") and cand_class in ("picture", "book"):
        return 0.04
    return 0.0


def pick_best(row_title: str, row_author: str, row_format: str, candidates):
    """candidates: dicts with title/subtitle/authors/format_class. → (cand, score)."""
    want_title, _ = query_terms(row_title)
    surname = query_terms("", row_author)[1] if row_author else ""
    best, best_key, best_score = None, None, 0.0
    for i, cand in enumerate(candidates):
        names = [cand.get("title") or ""]
        if cand.get("subtitle"):
            names.append(f"{cand['title']} {cand['subtitle']}")
        if cand.get("alt_title"):
            names.append(cand["alt_title"])
        score = title_score(want_title, names)
        if surname and cand.get("authors"):
            if _norm(surname) not in _norm(" ".join(cand["authors"])):
                score *= 0.7  # penalize, don't reject: cataloging varies
        score += _fmt_bonus(row_format or "", cand.get("format_class") or "")
        # Near-equal scores: prefer the edition with more copies on the shelf
        # (WebPAC candidates carry their items), then catalog relevance order.
        items = cand.get("items") or []
        n_avail = sum(1 for it in items if it.get("state") == "available")
        key = (round(score, 2), n_avail, len(items), -i)
        if best_key is None or key > best_key:
            best, best_key, best_score = cand, key, score
    if best_score >= MATCH_THRESHOLD:
        return best, round(best_score, 3)
    return None, round(best_score, 3)


# --- BiblioCommons (SCCLD, SJPL) ------------------------------------------------

def _bc_format_class(fmt: str) -> str:
    if fmt == "BOARD_BK":
        return "board"
    if fmt == "PICTURE_BOOK":
        return "picture"
    return "book"


def bc_parse_search(payload: dict) -> list[dict]:
    """Gateway search JSON → candidate list in result order (book formats only)."""
    bibs = payload.get("entities", {}).get("bibs", {})
    seen, out = set(), []
    for res in payload.get("catalogSearch", {}).get("results", []):
        ids = res.get("manifestations") or [res.get("representative")]
        for bid in ids:
            b = bibs.get(bid)
            if b is None or bid in seen:
                continue
            seen.add(bid)
            info = b.get("briefInfo", {})
            if info.get("format") not in BC_BOOK_FORMATS:
                continue
            out.append({
                "bib_id": bid,
                "title": info.get("title"),
                "subtitle": info.get("subtitle"),
                "alt_title": (info.get("multiscriptTitle") or {}).get("title")
                             if isinstance(info.get("multiscriptTitle"), dict)
                             else info.get("multiscriptTitle"),
                "authors": info.get("authors") or [],
                "format": info.get("format"),
                "format_class": _bc_format_class(info.get("format")),
                "year": info.get("publicationDate"),
                "call_number": info.get("callNumber"),
            })
    return out


def bc_item_state(avail: dict) -> str:
    if avail.get("libraryUseOnly"):
        return "reference"
    if avail.get("statusType") == "AVAILABLE":
        return "available"
    return "out"


def bc_parse_availability(payload: dict) -> list[dict]:
    """Gateway availability JSON → per-copy dicts for add_remote_availability."""
    items = []
    for it in payload.get("entities", {}).get("bibItems", {}).values():
        avail = it.get("availability", {})
        items.append({
            "branch": it.get("branchName")
                      or (it.get("branch") or {}).get("name") or "?",
            "collection": it.get("collection"),
            "call_number": it.get("callNumber"),
            "status": avail.get("libraryStatus") or avail.get("status"),
            "state": bc_item_state(avail),
        })
    return items


class BiblioCommons:
    """One BiblioCommons library (subdomain = 'sccl' or 'sjpl')."""

    def __init__(self, subdomain: str):
        self.subdomain = subdomain

    def search(self, query: str) -> list[dict]:
        url = (f"{GATEWAY}/{self.subdomain}/bibs/search?"
               f"query={urllib.parse.quote(query)}&searchType=smart")
        return bc_parse_search(json.loads(_get(url)))

    def search_fielded(self, title: str, surname: str = "") -> list[dict]:
        """Boolean field search — smart search drowns classics in spinoffs
        (25 'Very Hungry Caterpillar <theme>' board books before the original).
        """
        q = f"title:({re.sub(r'[():]', ' ', title)})"
        if surname:
            q += f" AND contributor:({surname})"
        url = (f"{GATEWAY}/{self.subdomain}/bibs/search?"
               f"query={urllib.parse.quote(q)}&searchType=bl")
        return bc_parse_search(json.loads(_get(url)))

    def availability(self, bib_id: str) -> list[dict]:
        url = f"{GATEWAY}/{self.subdomain}/bibs/{bib_id}/availability"
        return bc_parse_availability(json.loads(_get(url)))


# --- Mountain View classic WebPAC -----------------------------------------------

MVPL_BASE = "https://classiccatalog.mountainview.gov"

# Statuses that mean "walk in and it's on the shelf". Everything else (DUE …,
# ON HOLDSHELF, IN TRANSIT, MISSING, …) counts as out.
_WEBPAC_AVAILABLE = ("AVAILABLE", "CHECK SHELF", "NEW SHELF")
_WEBPAC_REFERENCE = ("LIB USE ONLY", "REFERENCE", "NON-CIRC")


def webpac_state(status: str) -> str:
    s = (status or "").upper()
    if any(m in s for m in _WEBPAC_REFERENCE):
        return "reference"
    if any(m in s for m in _WEBPAC_AVAILABLE):
        return "available"
    return "out"


def _strip_html(fragment: str) -> str:
    fragment = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    text = re.sub(r"\s+", " ", htmllib.unescape(fragment)).strip()
    # keyword-highlight spans leave 'I stink !' / 'McMullan , Kate' behind
    # (':' and ';' stay spaced — ISBD subtitle punctuation is ' : ')
    return re.sub(r"\s+([!?,.])", r"\1", text)


def _webpac_items(chunk: str) -> list[dict]:
    items = []
    for m in re.finditer(r'<tr\s+class="bibItemsEntry">(.*?)</tr>', chunk, re.S):
        cells = [_strip_html(c) for c in
                 re.findall(r"<td[^>]*>(.*?)(?:</td>|$)", m.group(1), re.S)]
        if len(cells) < 3:
            continue
        loc, call, status = cells[0], cells[1], cells[2]
        items.append({"branch": loc, "collection": None, "call_number": call,
                      "status": status, "state": webpac_state(status)})
    return items


def _webpac_fmt_class(media: str, items: list[dict]) -> str:
    blob = f"{media} " + " ".join(i["call_number"] or "" for i in items)
    if "board" in blob.lower():
        return "board"
    if "picture" in blob.lower():
        return "picture"
    return "book"


def _webpac_record_page(page: str) -> dict | None:
    """A single-hit keyword search jumps straight to the record view; parse that.

    The record page lays metadata out as bibInfoLabel/bibInfoData pairs and has
    the same bibItems table the results list embeds.
    """
    fields = {}
    for m in re.finditer(r'<td[^>]*class="bibInfoLabel">\s*([^<]+?)\s*</td>\s*'
                         r'<td[^>]*class="bibInfoData">(.*?)</td>', page, re.S):
        fields.setdefault(m.group(1).strip(), _strip_html(m.group(2)))
    title_stmt = fields.get("Title")
    if not title_stmt:
        return None
    # 'I stink! / Kate & Jim McMullan ; pictures by ...' → title / responsibility
    title = re.split(r"\s+/\s+", title_stmt)[0].strip()
    author = fields.get("Author", "")
    bid = None
    m = re.search(r"record=(b\d+)", page)
    if m:
        bid = m.group(1)
    items = _webpac_items(page)
    return {"bib_id": bid, "title": title, "subtitle": None, "alt_title": None,
            "authors": [author] if author else [],
            "format": fields.get("Material", "Book"),
            "format_class": _webpac_fmt_class("", items),
            "year": None, "items": items}


def webpac_parse_results(page: str) -> list[dict]:
    """Keyword-results page → candidates, each carrying its own item rows.

    The classic WebPAC inlines every hit's LOCATION/CALL #/STATUS table right in
    the results list, so one request answers both 'is it there' and 'where'.
    A single-hit search renders the record view instead — handled as one candidate.
    """
    chunks = re.split(r'class="briefCitRow"', page)[1:]
    if not chunks and 'class="bibItems"' in page:
        rec = _webpac_record_page(page)
        return [rec] if rec else []
    out = []
    for chunk in chunks:
        m = re.search(r'<span class="briefcitTitle">\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        title = _strip_html(m.group(1))
        author = ""
        m2 = re.search(r"</span>\s*<br\s*/?>\s*([^<]*)<br", chunk[m.end():], re.S)
        if m2:
            author = _strip_html(m2.group(1))
        bid = None
        m3 = re.search(r'name="save"\s+value="(b\d+)"', chunk)
        if m3:
            bid = m3.group(1)
        media = ""
        m4 = re.search(r'/screens/media_[a-z_]+\.gif"\s+alt="([^"]*)"', chunk)
        if m4:
            media = m4.group(1)
        # books only — an exact-title DVD or CD would otherwise outscore an
        # all-checked-out book edition
        if re.search(r"DVD|Blu-?ray|Compact Dis|\bCD\b|Audio|Video|Playaway|"
                     r"eBook|Magazine|Kit\b", media, re.I):
            continue
        items = _webpac_items(chunk)
        out.append({"bib_id": bid, "title": title, "subtitle": None,
                    "alt_title": None,
                    "authors": [author] if author else [],
                    "format": media or "Book",
                    "format_class": _webpac_fmt_class(media, items),
                    "year": None, "items": items})
    return out


class MountainView:
    """Mountain View Public Library via its classic (server-rendered) WebPAC."""

    def search(self, query: str) -> list[dict]:
        # This 2006-era WebPAC 502s on CJK in the query string — its Chinese
        # records are searchable through their romanization only.
        if _CJK.search(query):
            query = _pinyin(query)
        # Strip punctuation: '?' is a truncation wildcard to this WebPAC, and
        # 'see?' quietly turns an exact search into garbage matches.
        query = re.sub(r"[^A-Za-z0-9 ]+", " ", query).strip()
        url = (f"{MVPL_BASE}/search~S1/?searchtype=X"
               f"&searcharg={urllib.parse.quote_plus(query)}&SORT=D")
        # cold keyword searches here can take >30s; be patient
        page = _get(url, accept="text/html", timeout=75).decode("iso-8859-1",
                                                                "replace")
        return webpac_parse_results(page)

    def availability(self, bib_or_cand) -> list[dict]:
        # items ride along with the search results — no second request needed
        return bib_or_cand.get("items", [])


SYSTEMS = {
    "sccl": ("Santa Clara County Library District", lambda: BiblioCommons("sccl")),
    "sjpl": ("San José Public Library", lambda: BiblioCommons("sjpl")),
    "mvpl": ("Mountain View Public Library", MountainView),
}


# --- runner ---------------------------------------------------------------------

WANTLIST_ZH = "wantlist_zh.json"


def sync_wantlist(conn, path: str = WANTLIST_ZH) -> int:
    """Merge the hand-curated CJK want-list (checked into git) into `titles`.

    These books have no Peoria record (yet), so they get synthetic WANT: ids;
    they ride along in every Bay Area lookup and in the generated markdown.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh)
    except FileNotFoundError:
        return 0
    ts = datetime.now().isoformat(timespec="seconds")
    for e in entries:
        rid = "WANT:" + re.sub(r"\s+", "", e["title"])
        db.upsert_title(conn, rid, e["title"], ts,
                        {"author": e.get("author") or None,
                         "format": e.get("format"),
                         "isbns": e.get("isbn") or None})
    return len(entries)


def lookup_all(db_path: str, systems: list[str], limit: int = None,
               delay: float = 0.4, resume: bool = False,
               retry_misses: bool = False) -> None:
    conn = db.open_db(db_path)
    n = sync_wantlist(conn)
    if n:
        conn.commit()
        print(f"want-list: merged {n} entries from {WANTLIST_ZH}")
    rows = conn.execute(
        "SELECT record_id, title, author, format, isbns FROM titles ORDER BY title"
    ).fetchall()
    if limit:
        rows = rows[:limit]

    for system in systems:
        label, make_client = SYSTEMS[system]
        client = make_client()
        todo = rows
        if resume or retry_misses:
            # resume: skip anything already looked up (crash recovery).
            # retry_misses: also redo titles that were searched but never matched.
            q = "SELECT record_id FROM remote_bibs WHERE system = ?"
            if retry_misses:
                q += " AND bib_id IS NOT NULL"
            done = {r["record_id"] for r in conn.execute(q, (system,))}
            todo = [r for r in rows if r["record_id"] not in done]
        if not todo:
            print(f"[{system}] nothing to do")
            continue
        checked_at = datetime.now().isoformat(timespec="seconds")
        scrape_id = db.record_scrape(conn, kind="remote", checked_at=checked_at,
                                     query=f"{len(todo)} titles",
                                     source="bayarea_lookup", profile=system)
        print(f"[{system}] {label}: {len(todo)} titles")
        for i, row in enumerate(todo, 1):
            t, surname = query_terms(row["title"], row["author"])
            # CJK titles are specific enough alone; a Latin surname ANDed onto a
            # CJK query only knocks out legitimate hits
            query = t if _CJK.search(t) else f"{t} {surname}".strip()
            try:
                cands = client.search(query)
                time.sleep(delay)
                if not cands and query != t:
                    # author surname can over-constrain an AND search; retry bare
                    cands = client.search(t)
                    time.sleep(delay)
                if not cands and _CJK.search(t):
                    # some records are only findable through their romanization
                    cands = client.search(_pinyin(t))
                    time.sleep(delay)
                if not cands and row["isbns"]:
                    # last resort: the known edition's ISBN (candidates still
                    # have to pass title scoring, so a stale ISBN is harmless)
                    for isbn in re.findall(r"[0-9Xx]{10,13}", row["isbns"]):
                        cands = client.search(isbn)
                        time.sleep(delay)
                        if cands:
                            break
                best, score = pick_best(row["title"], row["author"],
                                        row["format"], cands)
                if score < 1.0 and hasattr(client, "search_fielded"):
                    # weak pick → try the exact-field search before settling
                    fcands = client.search_fielded(t, surname)
                    time.sleep(delay)
                    fbest, fscore = pick_best(row["title"], row["author"],
                                              row["format"], fcands)
                    if fbest is not None and fscore > score:
                        best, score = fbest, fscore
                if best is None:
                    with conn:
                        db.upsert_remote_bib(conn, system, row["record_id"],
                                             checked_at, None, score)
                    print(f"  {i:3}/{len(todo)} ✗ {row['title'][:50]!r} "
                          f"no match (best {score})")
                    continue
                items = client.availability(best if system == "mvpl"
                                            else best["bib_id"])
                if system != "mvpl":
                    time.sleep(delay)
                n_avail = sum(1 for it in items if it["state"] == "available")
                with conn:
                    db.upsert_remote_bib(conn, system, row["record_id"], checked_at,
                                         {"bib_id": best["bib_id"],
                                          "title": best["title"],
                                          "author": ", ".join(best["authors"]) or None,
                                          "format": best["format"],
                                          "year": best.get("year")}, score)
                    db.add_remote_availability(conn, scrape_id, system,
                                               row["record_id"], best["bib_id"],
                                               row["title"], items, checked_at)
                print(f"  {i:3}/{len(todo)} ✓ {row['title'][:50]!r} → "
                      f"{best['title'][:40]!r} ({best['format']}, {score}) "
                      f"{n_avail}/{len(items)} on shelf")
            except Exception as e:
                print(f"  {i:3}/{len(todo)} ! {row['title'][:50]!r} ERROR: {e}")
        conn.commit()

    conn.close()
    for path in report.write_bayarea(db_path):
        print(f"wrote {path}")


def probe(systems: list[str], query: str) -> None:
    """Ad-hoc one-title lookup; prints, records nothing."""
    for system in systems:
        label, make_client = SYSTEMS[system]
        client = make_client()
        print(f"== {label}")
        cands = client.search(query)
        best, score = pick_best(query, None, None, cands)
        if best is None:
            print(f"  no match (best score {score})")
            continue
        items = client.availability(best if system == "mvpl" else best["bib_id"])
        print(f"  {best['title']!r} ({best['format']}, score {score})")
        for it in items:
            mark = {"available": "✓", "reference": "·"}.get(it["state"], "✗")
            print(f"   {mark} {it['branch']}: {it['call_number']} — {it['status']}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Look up the want-list at SCCLD / SJPL / Mountain View.")
    ap.add_argument("--system", action="append", choices=sorted(SYSTEMS),
                    help="limit to a system (repeatable; default: all three)")
    ap.add_argument("--limit", type=int, help="only the first N titles (testing)")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="seconds between requests (default 0.4)")
    ap.add_argument("--resume", action="store_true",
                    help="skip titles already looked up in that system")
    ap.add_argument("--retry-misses", action="store_true",
                    help="like --resume, but also redo titles that never matched")
    ap.add_argument("--title", help="ad-hoc query: print availability, touch nothing")
    ap.add_argument("--db", default="peorialib.db")
    args = ap.parse_args(argv)

    systems = args.system or sorted(SYSTEMS)
    if args.title:
        probe(systems, args.title)
    else:
        lookup_all(args.db, systems, limit=args.limit, delay=args.delay,
                   resume=args.resume, retry_misses=args.retry_misses)


if __name__ == "__main__":
    main()
