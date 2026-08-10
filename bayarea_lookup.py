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

    uv run bayarea_lookup.py                          # all titles; systems run in parallel
    uv run bayarea_lookup.py --system sccl --limit 5  # quick spot check
    uv run bayarea_lookup.py --resume                 # only titles not yet looked up
    uv run bayarea_lookup.py --title "dear zoo"       # ad-hoc probe, prints only
    uv run bayarea_lookup.py --enrich                 # just the record-detail pass

Matching is fuzzy: we search title + author-surname, then score candidates by
normalized title similarity (works for the pinyin Chinese titles too, since
these catalogs index romanized fields). A title can legitimately not match —
that library just doesn't hold it — and that's recorded as bib_id NULL.

The best match anchors the title, and every other version of the same work in
the result set rides along (`remote_editions`): other physical formats and
printings, audiobooks (physical and digital), eBooks, and Chinese / French /
Spanish / Japanese editions. Movies and music are never candidates; digital
editions are linked but carry no shelf state (a license queue isn't a shelf).
"""
from __future__ import annotations

import argparse
import difflib
import glob
import gzip
import html as htmllib
import json
import re
import sys
import threading
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

# BiblioCommons formats we'll accept as "this book". BOOK_PCD / BOOK_CD / KIT
# cover the bilingual book-plus-audio kits on the list; audiobooks are physical
# audio; EBOOK/EAUDIOBOOK are tracked as digital editions (movies and music
# stay out).
BC_BOOK_FORMATS = {"BK", "BOARD_BK", "PICTURE_BOOK", "PAPERBACK", "LARGE_PRINT",
                   "BOOK_PCD", "BOOK_CD", "KIT",
                   "AB", "AUDIOBOOK_CD", "PLAYAWAY_AUDIOBOOK",
                   "EBOOK", "EAUDIOBOOK"}
_BC_AUDIO_FORMATS = {"AB", "AUDIOBOOK_CD", "PLAYAWAY_AUDIOBOOK",
                     "BOOK_CD", "BOOK_PCD"}
# Digital editions are listed and linked but have no shelf: their availability
# is a licensing queue (Libby/hoopla), not a branch, so no state is recorded.
DIGITAL_CLASSES = ("ebook", "eaudio")

# Language editions we surface alongside the main match (in addition to
# other physical formats of the same work).
EXTRA_LANGS = ("chi", "fre", "spa", "jpn")
# Versions tracked per (title, system) — bounds the per-title availability
# fetches when a classic is printed in a dozen editions.
MAX_EDITIONS = 8
# Extra same-language versions must be near-exact: series siblings score far
# above MATCH_THRESHOLD ('Panda Bear…What Do You See?' hits 0.773 against
# Polar Bear, ''…Caterpillar's Easter Colors' 0.771 against the original), while
# true editions of the same work sit at 0.98+ (subtitle variants included).
EDITION_MIN_RATIO = 0.9

# Below ~0.75 nearly everything is a lookalike (shared series prefixes, 'my
# first X' phrasing); the only legitimate sub-0.75 matches were exact titles
# dragged down by the author penalty, so that penalty is mild (0.85).
MATCH_THRESHOLD = 0.75
AUTHOR_MISMATCH_PENALTY = 0.85


# --- HTTP -----------------------------------------------------------------------

# Minimum spacing between requests to the SAME host, across threads: sccl and
# sjpl share gateway.bibliocommons.com, and two lookup threads interleaving on
# it without coordination earned CJK searches HTTP 403s.
_HOST_SPACING = 0.4
_host_gate = threading.Lock()
_host_last: dict = {}


def _pace(url: str) -> None:
    host = urllib.parse.urlsplit(url).netloc
    while True:
        with _host_gate:
            now = time.monotonic()
            wait = _host_last.get(host, 0.0) + _HOST_SPACING - now
            if wait <= 0:
                _host_last[host] = now
                return
        time.sleep(wait)


def _get(url: str, accept: str = "application/json", tries: int = 3,
         timeout: float = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": accept})
    last_err = None
    for attempt in range(tries):
        _pace(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except Exception as e:  # URLError, HTTPError, timeout
            last_err = e
            if getattr(e, "code", None) in (403, 429):  # throttled: back off hard
                time.sleep(20 * (attempt + 1))
            else:
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
                    # a pair that dropped a subtitle must be near-exact on what
                    # remains — 'Chicka Chicka I love you' vs the stem of
                    # 'Chicka chicka you you : a mirror book' scores 0.84 on
                    # shared prefix alone, and that's a different book
                    if r < 0.9:
                        continue
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


def _author_matches(surname: str, cand) -> bool:
    return bool(surname) and bool(cand.get("authors")) and \
        _norm(surname) in _norm(" ".join(cand["authors"]))


def _cand_score(want_title: str, surname: str, cand) -> float:
    """Title similarity for one candidate, with the author-mismatch damp."""
    names = [cand.get("title") or ""]
    if cand.get("subtitle"):
        names.append(f"{cand['title']} {cand['subtitle']}")
    if cand.get("alt_title"):
        names.append(cand["alt_title"])
    score = title_score(want_title, names)
    if surname and cand.get("authors") and not _author_matches(surname, cand):
        # penalize, don't reject: 'Ten apples up on top!' is cataloged
        # under LeSieg, not Seuss
        score *= AUTHOR_MISMATCH_PENALTY
    return score


def pick_best(row_title: str, row_author: str, row_format: str, candidates,
              lang: str = None, enforce_lang: bool = True):
    """candidates: dicts with title/subtitle/authors/format_class. → (cand, score).

    A want with `lang` set ('fre', 'chi') only accepts candidates in that
    language, and at a stricter threshold — otherwise 'Cher zoo' happily takes
    the English Dear Zoo, and 'T'choupi va sur le pot' any other T'choupi.
    enforce_lang=False keeps just the stricter threshold, for catalogs whose
    search results don't say what language a record is in (LINK+).
    """
    if lang and enforce_lang:
        candidates = [c for c in candidates if c.get("language") == lang]
    threshold = 0.8 if lang else MATCH_THRESHOLD
    want_title, _ = query_terms(row_title)
    surname = query_terms("", row_author)[1] if row_author else ""
    best, best_key, best_score = None, None, 0.0
    for i, cand in enumerate(candidates):
        score = _cand_score(want_title, surname, cand)
        score += _fmt_bonus(row_format or "", cand.get("format_class") or "")
        # Near-equal scores: prefer a physical edition (the primary anchors the
        # shelf matrix; digital rides along as an extra), then the edition with
        # more copies on the shelf (WebPAC candidates carry their items), then
        # catalog relevance order.
        items = cand.get("items") or []
        n_avail = sum(1 for it in items if it.get("state") == "available")
        key = (round(score, 2),
               (cand.get("format_class") or "") not in DIGITAL_CLASSES,
               n_avail, len(items), -i)
        if best_key is None or key > best_key:
            best, best_key, best_score = cand, key, score
    if best_score >= threshold:
        return best, round(best_score, 3)
    return None, round(best_score, 3)


def pick_all(row_title: str, row_author: str, row_format: str, candidates,
             lang: str = None, enforce_lang: bool = True,
             max_editions: int = MAX_EDITIONS):
    """(primary, score, editions) — the best match plus every other version of
    the same work worth showing: other physical formats/printings that clear
    the normal title bar ('edition'), translations in EXTRA_LANGS
    ('translation' — a translated title can't fuzzy-match the original, so
    same-author + language stands in), and physical audiobooks ('audio' —
    compilations like 'Brown bear & friends' carry the story retitled).

    The loose rules only trust candidates from an AND-semantics search
    (cand['strict']: WebPAC keyword results, BC's fielded search — where a
    translation surfaces via its 'Translation of:' note / uniform title), so a
    fuzzy smart-search result can't drift to the author's *other* works. Even
    then, spinoff translations can sneak in ('Translation of: The Very Hungry
    Caterpillar's Easter Colors' contains the original title's every word), so
    only one translation per (language, format) is kept — the one whose title
    length sits closest to the want's, and spinoff titles run long.
    Editions include the primary (kind='primary').
    """
    primary, score = pick_best(row_title, row_author, row_format, candidates,
                               lang, enforce_lang)
    if primary is None:
        return None, score, []
    want_title, _ = query_terms(row_title)
    surname = query_terms("", row_author)[1] if row_author else ""
    editions = [dict(primary, kind="primary", match_score=score)]
    seen = {primary.get("bib_id")}
    trans = {}  # (language, format_class) -> (title_len_diff, cand, score)
    for cand in candidates:
        bid = cand.get("bib_id")
        if not bid or bid in seen:
            continue
        clang = cand.get("language")
        s = round(_cand_score(want_title, surname, cand), 3)
        if s >= EDITION_MIN_RATIO and (lang is None or not enforce_lang
                                       or clang in (lang, None)):
            seen.add(bid)
            editions.append(dict(cand, kind="edition", match_score=s))
        elif cand.get("strict") and \
                cand.get("format_class") in ("audio", "eaudio") and \
                _author_matches(surname, cand) and \
                (lang is None or clang in (lang, None)):
            seen.add(bid)
            editions.append(dict(cand, kind="audio", match_score=s))
        elif cand.get("strict") and lang is None and clang in EXTRA_LANGS and \
                _author_matches(surname, cand):
            key = (clang, cand.get("format_class"))
            diff = abs(len(_norm(cand.get("title") or "")) - len(_norm(want_title)))
            if key not in trans or diff < trans[key][0]:
                trans[key] = (diff, cand, s)
    for diff, cand, s in trans.values():
        if cand["bib_id"] not in seen:
            seen.add(cand["bib_id"])
            editions.append(dict(cand, kind="translation", match_score=s))
    if len(editions) > max_editions:
        # translations and audiobooks are the rare finds; the Nth same-language
        # printing is what gets cut
        rest = sorted(editions[1:], key=lambda e:
                      {"translation": 0, "audio": 1, "edition": 2}[e["kind"]])
        editions = editions[:1] + rest[:max_editions - 1]
    return dict(editions[0]), score, editions


# --- BiblioCommons (SCCLD, SJPL) ------------------------------------------------

def _bc_format_class(fmt: str) -> str:
    if fmt == "BOARD_BK":
        return "board"
    if fmt == "PICTURE_BOOK":
        return "picture"
    if fmt in _BC_AUDIO_FORMATS:
        return "audio"
    if fmt == "EBOOK":
        return "ebook"
    if fmt == "EAUDIOBOOK":
        return "eaudio"
    return "book"


def bc_parse_search(payload: dict, strict: bool = False) -> list[dict]:
    """Gateway search JSON → candidate list in result order (book formats only).

    strict marks candidates from an AND-semantics query (the fielded search) —
    the only ones pick_all's author-anchored rules are allowed to trust.
    """
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
                "strict": strict,
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
                "language": info.get("primaryLanguage"),
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
        return bc_parse_search(json.loads(_get(url)), strict=True)

    def availability(self, bib_id: str) -> list[dict]:
        url = f"{GATEWAY}/{self.subdomain}/bibs/{bib_id}/availability"
        return bc_parse_availability(json.loads(_get(url)))


def bc_marc_fields(page: str) -> dict:
    """The classic MARC display (item/catalogue_info) → {tag: [field data]}."""
    out = {}
    for m in re.finditer(r'class="marcTag"><strong>(\d+)</strong></td>.*?'
                         r'class="marcTagData">(.*?)</td>', page, re.S):
        out.setdefault(m.group(1), []).append(
            htmllib.unescape(m.group(2)).strip())
    return out


def _marc_subfields(data: str) -> dict:
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"\$([a-z0-9])([^$]*)", data)}


_BC_BIB_ID = re.compile(r"^S(\d+)C(\d+)$")


def bc_details(subdomain: str, bib_id: str) -> dict:
    """contents (MARC 505) + original title (240 uniform title, else a
    'Translation of' 500/765 note) — from the classic MARC display, the one
    server-rendered detail view BiblioCommons still has. The gateway search
    payload carries neither."""
    m = _BC_BIB_ID.match(bib_id or "")
    if not m:
        return {}
    url = (f"https://{subdomain}.bibliocommons.com/item/catalogue_info/"
           f"{m.group(2)}{m.group(1)}")
    page = _get(url, accept="text/html", timeout=60).decode("utf-8", "replace")
    return bc_details_from_page(page)


def bc_details_from_page(page: str) -> dict:
    marc = bc_marc_fields(page)
    parts = []
    for d in marc.get("505", []):
        t = re.sub(r"\$[a-z0-9]", " ", d)
        t = re.sub(r"\s*--\s*", "; ", t)
        t = re.sub(r"\s+", " ", t).strip(" ;$")
        if t:
            parts.append(t)
    orig = ""
    for d in marc.get("240", []):
        orig = _marc_subfields(d).get("a", "").strip(" /:;,.$")
        if orig:
            break
    if not orig:
        for d in marc.get("500", []) + marc.get("765", []):
            m2 = re.search(r"Translation of:?\s*(.+)",
                           re.sub(r"\$[a-z0-9]", " ", d), re.I)
            if m2:
                orig = m2.group(1).strip(" .$")
                break
    return {"contents": "; ".join(parts), "orig_title": orig}


# --- Mountain View classic WebPAC -----------------------------------------------

MVPL_BASE = "https://classiccatalog.mountainview.gov"

# Statuses that mean "walk in and it's on the shelf". Everything else (DUE …,
# ON HOLDSHELF, IN TRANSIT, MISSING, …) counts as out.
_WEBPAC_AVAILABLE = ("AVAILABLE", "CHECK SHELF", "NEW SHELF")
_WEBPAC_REFERENCE = ("LIB USE ONLY", "REFERENCE", "NON-CIRC")
# checked before the available markers: 'UNAVAILABLE' (LINK+) would otherwise
# hit the 'AVAILABLE' substring
_WEBPAC_OUT = ("UNAVAILABLE", "NOT AVAILABLE")


def webpac_state(status: str) -> str:
    s = (status or "").upper()
    if any(m in s for m in _WEBPAC_REFERENCE):
        return "reference"
    if any(m in s for m in _WEBPAC_OUT):
        return "out"
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


# Digital editions we keep — checked first: 'eAudiobook' contains 'Audiobook',
# which contains 'Audio'.
_DIGITAL_MEDIA = re.compile(r"e-?Book|e-?Audio|Downloadable", re.I)
# Physical audiobooks are a format we keep (classed 'audio'); checked before
# _NONBOOK_MEDIA because 'Audiobook' would otherwise trip its 'Audio'.
_AUDIO_MEDIA = re.compile(r"Audiobook|Book on CD|CD Book|Playaway(?!\s*Video)",
                          re.I)
# an exact-title DVD, soundtrack CD, or eBook must never satisfy a want,
# no matter how well the title scores
_NONBOOK_MEDIA = re.compile(r"DVD|Blu-?ray|Compact Dis|\bCD\b|Audio|Video|"
                            r"Playaway|eBook|Magazine|Kit\b|videodisc|sound disc",
                            re.I)
_NONBOOK_SHELF = re.compile(r"Movies|Music", re.I)


def _webpac_nonbook(media: str, items: list[dict]) -> bool:
    m = media or ""
    if not _DIGITAL_MEDIA.search(m) and not _AUDIO_MEDIA.search(m) \
            and _NONBOOK_MEDIA.search(m):
        return True
    # a record whose every copy shelves under Movies/Music is one of those,
    # whatever it calls itself
    return bool(items) and all(_NONBOOK_SHELF.search(i.get("branch") or "")
                               for i in items)


# MVPL flags language in the call number: 'J FRENCH J P TISON', 'J CHINESE …'
_WEBPAC_LANGS = {"FRENCH": "fre", "CHINESE": "chi", "SPANISH": "spa",
                 "JAPANESE": "jpn", "KOREAN": "kor", "RUSSIAN": "rus",
                 "GERMAN": "ger", "HINDI": "hin"}


def _webpac_language(items: list[dict]) -> str | None:
    blob = " ".join((i.get("call_number") or "").upper() for i in items)
    for marker, code in _WEBPAC_LANGS.items():
        if marker in blob:
            return code
    return None


def _webpac_fmt_class(media: str, items: list[dict]) -> str:
    if _DIGITAL_MEDIA.search(media or ""):
        return "eaudio" if re.search("audio", media, re.I) else "ebook"
    if _AUDIO_MEDIA.search(media or ""):
        return "audio"
    blob = f"{media} " + " ".join(i["call_number"] or "" for i in items)
    if "board" in blob.lower():
        return "board"
    if "picture" in blob.lower():
        return "picture"
    return "book"


def _webpac_fields(page: str) -> dict:
    """Record-view metadata: bibInfoLabel → cleaned bibInfoData text."""
    fields = {}
    for m in re.finditer(r'<td[^>]*class="bibInfoLabel">\s*([^<]+?)\s*</td>\s*'
                         r'<td[^>]*class="bibInfoData">(.*?)</td>', page, re.S):
        fields.setdefault(m.group(1).strip(), _strip_html(m.group(2)))
    return fields


def _webpac_record_page(page: str) -> dict | None:
    """A single-hit keyword search jumps straight to the record view; parse that.

    The record page lays metadata out as bibInfoLabel/bibInfoData pairs and has
    the same bibItems table the results list embeds.
    """
    fields = _webpac_fields(page)
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
    if _webpac_nonbook(fields.get("Material", ""), items):
        return None
    return {"bib_id": bid, "strict": True, "title": title, "subtitle": None,
            "alt_title": None, "authors": [author] if author else [],
            "format": fields.get("Material", "Book"),
            "format_class": _webpac_fmt_class(fields.get("Material", ""), items),
            "year": None, "items": items,
            "language": _webpac_language(items)}


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
        items = _webpac_items(chunk)
        if _webpac_nonbook(media, items):
            continue
        out.append({"bib_id": bid, "strict": True, "title": title,
                    "subtitle": None, "alt_title": None,
                    "authors": [author] if author else [],
                    "format": media or "Book",
                    "format_class": _webpac_fmt_class(media, items),
                    "year": None, "items": items,
                    "language": _webpac_language(items)})
    return out


def webpac_query(query: str) -> str:
    """What this catalog's search box can actually digest.

    - CJK 502s the 2006-era server — Chinese records are searchable only
      through their romanization.
    - Diacritics must be folded, not stripped ('Bébés' → 'bebes', not 'b b s').
    - Apostrophes must be joined, not split: the III keyword index matches
      "can't" to 'cant', while a split leaves a stray 't' (or the 'T' of
      T'choupi) that ANDs the search down to nothing.
    - Other punctuation goes: '?' is a truncation wildcard here, and 'see?'
      quietly turns an exact search into garbage matches.
    """
    if _CJK.search(query):
        query = _pinyin(query)
    query = unicodedata.normalize("NFKD", query)
    query = "".join(c for c in query if not unicodedata.combining(c))
    query = re.sub(r"['’]", "", query)
    query = re.sub(r"[^A-Za-z0-9 ]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    # mid-query 'not' is a boolean operator here — 'But not the hippopotamus'
    # finds nothing. A *leading* 'not' has nothing to negate and stays a term.
    toks = query.split()
    if toks:
        query = " ".join([toks[0]] + [t for t in toks[1:] if t.lower() != "not"])
    return query


class MountainView:
    """Mountain View Public Library via its classic (server-rendered) WebPAC."""

    def search(self, query: str) -> list[dict]:
        url = (f"{MVPL_BASE}/search~S1/?searchtype=X"
               f"&searcharg={urllib.parse.quote_plus(webpac_query(query))}&SORT=D")
        # cold keyword searches here can take >30s; be patient
        page = _get(url, accept="text/html", timeout=75).decode("iso-8859-1",
                                                                "replace")
        return webpac_parse_results(page)

    def availability(self, bib_or_cand) -> list[dict]:
        # items ride along with the search results — no second request needed
        return bib_or_cand.get("items", [])


def mvpl_details(bib_id: str) -> dict:
    """contents + 'Translation of' original title from the classic record view."""
    page = _get(f"{MVPL_BASE}/record={bib_id}", accept="text/html",
                timeout=75).decode("iso-8859-1", "replace")
    return mvpl_details_from_page(page)


def mvpl_details_from_page(page: str) -> dict:
    fields = _webpac_fields(page)
    contents = re.sub(r"\s*--\s*", "; ",
                      fields.get("Contents", "")).strip(" ;")
    orig = ""
    # scan every metadata cell: the translation note is one of several 'Note'
    # rows and _webpac_fields keeps only the first per label
    for m in re.finditer(r'class="bibInfoData">(.*?)</td>', page, re.S):
        m2 = re.match(r"Translation of:?\s*(.+)", _strip_html(m.group(1)), re.I)
        if m2:
            orig = m2.group(1).strip(" .")
            break
    return {"contents": contents, "orig_title": orig}


# --- LINK+ (INN-Reach union catalog) --------------------------------------------

LINKPLUS_BASE = "https://csul.iii.com"


def linkplus_parse_results(page: str) -> list[dict]:
    """LINK+ results list → candidates (no items; holdings need a second fetch).

    Same WebPAC family as Mountain View but the 2009-era INN-Reach skin:
    div.briefcitRow (lower-case c), h2.briefcitTitle, no media icons — non-book
    formats are visible only in the description line ('1 videodisc …').
    """
    out = []
    for chunk in re.split(r'class="briefcitRow"', page)[1:]:
        m = re.search(r'<h2 class="briefcitTitle">\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        title = _strip_html(m.group(1))
        author = ""
        m2 = re.search(r"<br\s*>\s*([^<]*)<br", chunk[m.end():], re.S)
        if m2:
            author = _strip_html(m2.group(1))
        m3 = re.search(r'name="save"\s+value="(b\d+)"', chunk)
        bid = m3.group(1) if m3 else None
        desc = _strip_html(chunk[m.end():m.end() + 800])
        if _NONBOOK_MEDIA.search(desc):
            continue
        out.append({"bib_id": bid, "strict": True, "title": title,
                    "subtitle": None, "alt_title": None,
                    "authors": [author] if author else [],
                    "format": "Book", "format_class": "book",
                    "year": None, "language": None})
    return out


def linkplus_parse_holdings(page: str) -> list[dict]:
    """centralDetailHoldings rows → per-copy dicts (branch = owning library)."""
    items = []
    m = re.search(r'<table[^>]*class="centralDetailHoldings".*?</table>', page, re.S)
    if not m:
        return items
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S):
        cells = [_strip_html(c) for c in
                 re.findall(r"<td[^>]*>(.*?)(?:</td>|$)", row.group(1), re.S)]
        if len(cells) < 5 or not cells[0]:
            continue
        library, shelf, _link, call, status = cells[:5]
        items.append({"branch": library, "collection": shelf,
                      "call_number": call, "status": status,
                      "state": webpac_state(status)})
    return items


class LinkPlus:
    """LINK+ union catalog: any hit is requestable for pickup at a member library."""

    def search(self, query: str) -> list[dict]:
        url = (f"{LINKPLUS_BASE}/search~S0/?searchtype=X"
               f"&searcharg={urllib.parse.quote_plus(webpac_query(query))}&SORT=D")
        # unlike MVPL's latin-1 WebPAC, the INN-Reach central serves UTF-8
        page = _get(url, accept="text/html", timeout=75).decode("utf-8",
                                                                "replace")
        cands = linkplus_parse_results(page)
        if not cands and "centralDetailHoldings" in page:
            rec = _webpac_record_page(page)  # single hit → detail view
            if rec:
                rec["items"] = linkplus_parse_holdings(page)
                return [rec]
        return cands

    def availability(self, cand) -> list[dict]:
        if isinstance(cand, dict):
            if cand.get("items") is not None:
                return cand["items"]
            cand = cand["bib_id"]
        bib = cand
        url = (f"{LINKPLUS_BASE}/search?/.{bib}/.{bib}/1,1,1,B/"
               f"detlframeset~{bib}&FF=&1,0,")
        page = _get(url, accept="text/html", timeout=75).decode("utf-8",
                                                                "replace")
        return linkplus_parse_holdings(page)


SYSTEMS = {
    "sccl": ("Santa Clara County Library District", lambda: BiblioCommons("sccl")),
    "sjpl": ("San José Public Library", lambda: BiblioCommons("sjpl")),
    "mvpl": ("Mountain View Public Library", MountainView),
    "linkplus": ("LINK+ union catalog", LinkPlus),
}

# Politeness per host, applied within each system's own (serial) thread —
# LINK+ 429s below a full second; the others tolerate a brisker pace.
SYSTEM_DELAYS = {"sccl": 0.4, "sjpl": 0.4, "mvpl": 0.4, "linkplus": 1.0}


# --- runner ---------------------------------------------------------------------

WANTLIST_GLOB = "wantlist_*.json"  # wantlist_zh.json, wantlist_fr.json, …
EXCLUDE_FILE = "wantlist_exclude.json"


def load_excludes(path: str = EXCLUDE_FILE) -> set:
    """Exact DB titles to leave out of remote lookups (Peoria-only shelf finds)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh).get("titles", []))
    except FileNotFoundError:
        return set()


def sync_wantlist(conn, path: str) -> int:
    """Merge a hand-curated want-list file (checked into git) into `titles`.

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


def wantlist_langs() -> dict:
    """{record_id: lang} for every want-list entry that pins a language."""
    langs = {}
    for wl in sorted(glob.glob(WANTLIST_GLOB)):
        if wl == EXCLUDE_FILE:
            continue
        with open(wl, encoding="utf-8") as fh:
            for e in json.load(fh):
                if e.get("lang"):
                    langs["WANT:" + re.sub(r"\s+", "", e["title"])] = e["lang"]
    return langs


def lookup_all(db_path: str, systems: list[str], limit: int = None,
               delay: float = None, resume: bool = False,
               retry_misses: bool = False) -> None:
    """Look the want-list up at every requested system — systems in parallel
    (one thread + one DB connection each; each host still gets serial,
    delay-spaced requests), then the detail-enrichment pass, then the reports.
    """
    conn = db.open_db(db_path)
    for wl in sorted(glob.glob(WANTLIST_GLOB)):
        if wl == EXCLUDE_FILE:
            continue
        n = sync_wantlist(conn, wl)
        if n:
            conn.commit()
            print(f"want-list: merged {n} entries from {wl}")
    rows = conn.execute(
        "SELECT record_id, title, author, format, isbns FROM titles ORDER BY title"
    ).fetchall()
    excludes = load_excludes()
    if excludes:
        before = len(rows)
        rows = [r for r in rows if r["title"] not in excludes]
        print(f"want-list: excluding {before - len(rows)} titles "
              f"({EXCLUDE_FILE})")
    if limit:
        rows = rows[:limit]
    conn.close()
    langs = wantlist_langs()

    threads = []
    for system in systems:
        d = delay if delay is not None else SYSTEM_DELAYS.get(system, 0.4)
        t = threading.Thread(target=_lookup_system, name=system,
                             args=(db_path, system, rows, langs, d,
                                   resume, retry_misses))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    n = enrich_editions(db_path, systems, delay if delay is not None else 0.5)
    if n:
        print(f"details: enriched {n} compilation/translation records")
    for path in report.write_bayarea(db_path):
        print(f"wrote {path}")


def _lookup_system(db_path: str, system: str, rows, langs: dict, delay: float,
                   resume: bool, retry_misses: bool) -> None:
    conn = db.open_db(db_path)
    try:
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
            return
        checked_at = datetime.now().isoformat(timespec="seconds")
        with conn:  # commit at once — an open write transaction stalls the others
            scrape_id = db.record_scrape(conn, kind="remote",
                                         checked_at=checked_at,
                                         query=f"{len(todo)} titles",
                                         source="bayarea_lookup", profile=system)
        print(f"[{system}] {label}: {len(todo)} titles")
        for i, row in enumerate(todo, 1):
            lang = langs.get(row["record_id"])
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
                enforce = system != "linkplus"
                if hasattr(client, "search_fielded"):
                    # always pool in the boolean field search: it rescues weak
                    # smart-search picks AND is the only strict source for
                    # translations/audiobooks (pick_all trusts nothing else)
                    fcands = client.search_fielded(t, surname)
                    time.sleep(delay)
                    known = {c.get("bib_id") for c in cands}
                    fids = {c.get("bib_id") for c in fcands}
                    for c in cands:      # a bib in both sets is strict
                        if c.get("bib_id") in fids:
                            c["strict"] = True
                    cands = cands + [c for c in fcands
                                     if c.get("bib_id") not in known]
                # LINK+ holdings cost a page fetch per edition; keep it tight
                max_ed = 4 if system == "linkplus" else MAX_EDITIONS
                best, score, editions = pick_all(row["title"], row["author"],
                                                 row["format"], cands, lang,
                                                 enforce, max_ed)
                if best is None:
                    with conn:
                        db.upsert_remote_bib(conn, system, row["record_id"],
                                             checked_at, None, score)
                        db.replace_remote_editions(conn, system,
                                                   row["record_id"], [],
                                                   checked_at)
                    print(f"[{system}] {i:3}/{len(todo)} ✗ {row['title'][:50]!r} "
                          f"no match (best {score})")
                    continue
                with conn:
                    db.upsert_remote_bib(conn, system, row["record_id"], checked_at,
                                         {"bib_id": best["bib_id"],
                                          "title": best["title"],
                                          "author": ", ".join(best["authors"]) or None,
                                          "format": best["format"],
                                          "year": best.get("year")}, score)
                    db.replace_remote_editions(conn, system, row["record_id"],
                                               editions, checked_at)
                n_avail = n_items = 0
                for ed in editions:
                    if (ed.get("format_class") or "") in DIGITAL_CLASSES:
                        continue  # linked, but a license queue isn't a shelf
                    items = client.availability(ed if system in ("mvpl", "linkplus")
                                                else ed["bib_id"])
                    if system != "mvpl":
                        time.sleep(delay)
                    n_avail += sum(1 for it in items if it["state"] == "available")
                    n_items += len(items)
                    with conn:
                        db.add_remote_availability(conn, scrape_id, system,
                                                   row["record_id"], ed["bib_id"],
                                                   row["title"], items, checked_at)
                extras = ", ".join(
                    "+" + ((e.get("language") or "?") if e["kind"] == "translation"
                           else e.get("format_class") or "ed")
                    for e in editions if e["kind"] != "primary")
                print(f"[{system}] {i:3}/{len(todo)} ✓ {row['title'][:50]!r} → "
                      f"{best['title'][:40]!r} ({best['format']}, {score}) "
                      f"{n_avail}/{n_items} on shelf"
                      + (f" [{extras}]" if extras else ""))
            except Exception as e:
                print(f"[{system}] {i:3}/{len(todo)} ! {row['title'][:50]!r} "
                      f"ERROR: {e}")
        conn.commit()
    finally:
        conn.close()


def enrich_editions(db_path: str, systems, delay: float = 0.5) -> int:
    """Fetch record details for the versions that are other works: what a
    compilation contains (505 contents), what a translation is a translation
    of (uniform title / note). Idempotent — only rows never fetched are hit —
    and parallel per system, like the lookups.
    """
    conn = db.open_db(db_path)
    todo = [r for r in db.unenriched_editions(conn) if r["system"] in systems]
    conn.close()
    by_sys = {}
    for r in todo:
        if r["system"] != "linkplus":   # linkplus carries neither kind
            by_sys.setdefault(r["system"], []).append(r)
    done = []

    def work(system, rows_):
        c = db.open_db(db_path)
        try:
            for r in rows_:
                try:
                    d = (bc_details(system, r["bib_id"])
                         if system in ("sccl", "sjpl")
                         else mvpl_details(r["bib_id"]))
                    time.sleep(delay)
                except Exception as e:
                    print(f"  enrich ! {system}/{r['bib_id']}: {e}")
                    continue
                with c:
                    db.set_edition_details(c, system, r["record_id"],
                                           r["bib_id"], d.get("contents"),
                                           d.get("orig_title"))
                done.append(1)
        finally:
            c.close()

    threads = [threading.Thread(target=work, args=(s, rs), name=f"enrich-{s}")
               for s, rs in by_sys.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(done)


def probe(systems: list[str], query: str) -> None:
    """Ad-hoc one-title lookup; prints, records nothing."""
    for system in systems:
        label, make_client = SYSTEMS[system]
        client = make_client()
        print(f"== {label}")
        cands = client.search(query)
        if hasattr(client, "search_fielded"):   # same union a real run pools
            fcands = client.search_fielded(query)
            known = {c.get("bib_id") for c in cands}
            fids = {c.get("bib_id") for c in fcands}
            for c in cands:
                if c.get("bib_id") in fids:
                    c["strict"] = True
            cands += [c for c in fcands if c.get("bib_id") not in known]
        best, score, editions = pick_all(query, None, None, cands)
        if best is None:
            print(f"  no match (best score {score})")
            continue
        for ed in editions:
            items = client.availability(ed if system in ("mvpl", "linkplus")
                                        else ed["bib_id"])
            tag = ed["kind"] + (f":{ed['language']}" if ed.get("language") else "")
            print(f"  {ed['title']!r} [{tag}] ({ed['format']}, "
                  f"score {ed['match_score']})")
            for it in items:
                mark = {"available": "✓", "reference": "·"}.get(it["state"], "✗")
                print(f"   {mark} {it['branch']}: {it['call_number']} — "
                      f"{it['status']}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Look up the want-list at SCCLD / SJPL / Mountain View.")
    ap.add_argument("--system", action="append", choices=sorted(SYSTEMS),
                    help="limit to a system (repeatable; default: all four)")
    ap.add_argument("--limit", type=int, help="only the first N titles (testing)")
    ap.add_argument("--delay", type=float, default=None,
                    help="seconds between requests, same for every system "
                         "(default: per-system — 0.4, but 1.0 for LINK+)")
    ap.add_argument("--resume", action="store_true",
                    help="skip titles already looked up in that system")
    ap.add_argument("--retry-misses", action="store_true",
                    help="like --resume, but also redo titles that never matched")
    ap.add_argument("--title", help="ad-hoc query: print availability, touch nothing")
    ap.add_argument("--enrich", action="store_true",
                    help="only fetch missing compilation/translation details, "
                         "then rewrite the reports")
    ap.add_argument("--db", default="peorialib.db")
    args = ap.parse_args(argv)

    systems = args.system or sorted(SYSTEMS)
    if args.title:
        probe(systems, args.title)
    elif args.enrich:
        n = enrich_editions(args.db, systems,
                            delay=args.delay if args.delay is not None else 0.5)
        print(f"details: enriched {n} records")
        for path in report.write_bayarea(args.db):
            print(f"wrote {path}")
    else:
        lookup_all(args.db, systems, limit=args.limit, delay=args.delay,
                   resume=args.resume, retry_misses=args.retry_misses)


if __name__ == "__main__":
    main()
