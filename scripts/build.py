#!/usr/bin/env python3
"""StepTube data builder.

Reads channels.yml + classics.yml, pulls each channel's public RSS feed
(no API key needed), filters out Shorts / live / premiere noise, flags
clickbait, merges with the existing library so it grows over time, and writes
site/data/videos.json for the static front-end.

Usage:
    python scripts/build.py                # normal (network) build
    python scripts/build.py --offline      # rebuild from existing data only
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"
VIDEOS_JSON = DATA / "videos.json"
IDS_JSON = DATA / "channel_ids.json"
BACKFILL_JSON = DATA / "backfill.json"   # channels whose full archive was already fetched
REPORT_JSON = DATA / "build_report.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Skip the EU consent interstitial so channel pages contain real HTML.
COOKIE = "CONSENT=YES+cb; SOCS=CAI"

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

TITLES_VERSION = 3       # bump to re-fetch non-English channels' original titles
MAX_PER_CHANNEL = 1000
VIEWS_VERSION = 5        # bump to re-fetch channels whose stored videos lack view counts
DUR_VERSION = 1          # bump to re-fetch channels whose stored videos lack durations
CC_VERSION = 3           # bump to re-run the deep Claude Code backfill  # library cap per channel (oldest dropped first)
DESC_CHARS = 280

# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401
        return None


_no_redirect_opener = urllib.request.build_opener(NoRedirect)


def _accept_lang(hl: str) -> str:
    return "en-US,en;q=0.8" if hl == "en" else f"{hl},{hl}-RU;q=0.9,en;q=0.5" if hl == "ru" else f"{hl},en;q=0.5"


def http_get(url: str, timeout: float = 20, retries: int = 3, hl: str = "en") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": COOKIE,
                                               "Accept-Language": _accept_lang(hl)})
    last: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # network hiccup
            last = e
        time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def is_short(video_id: str) -> bool | None:
    """youtube.com/shorts/<id> answers 200 for Shorts and redirects otherwise."""
    req = urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}",
                                 method="HEAD", headers={"User-Agent": UA, "Cookie": COOKIE})
    try:
        with _no_redirect_opener.open(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return False
        return None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Channel resolution
# --------------------------------------------------------------------------

ID_PATTERNS = [
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
    re.compile(r'<meta itemprop="identifier" content="(UC[\w-]{22})"'),
]


def resolve_handle(handle: str) -> str | None:
    h = handle.lstrip("@")
    try:
        page = http_get(f"https://www.youtube.com/@{urllib.parse.quote(h)}")
    except Exception:
        return None
    for pat in ID_PATTERNS:
        m = pat.search(page)
        if m:
            return m.group(1)
    return None


CHANNEL_RENDERER = re.compile(
    r'"channelRenderer":\{"channelId":"(UC[\w-]{22})","title":\{"simpleText":"((?:[^"\\]|\\.)*)"')


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", s).casefold())


def resolve_name(name: str) -> tuple[str, str] | None:
    """Find a channel by display name via YouTube's channel-filtered search.

    Prefers an exact (normalized) title match among the results, else the top hit.
    Returns (channel_id, matched_title)."""
    q = urllib.parse.urlencode({"search_query": name, "sp": "EgIQAg=="})
    try:
        page = http_get(f"https://www.youtube.com/results?{q}")
    except Exception:
        return None
    hits = [(cid, json.loads(f'"{t}"')) for cid, t in CHANNEL_RENDERER.findall(page)]
    if not hits:
        return None
    want = _norm(name)
    for cid, t in hits:
        if _norm(t) == want:
            return cid, t
    for cid, t in hits:  # names truncated in the app ("…"): the real title is longer
        if len(want) >= 8 and _norm(t).startswith(want):
            return cid, t
    return hits[0]


# --------------------------------------------------------------------------
# Title hygiene
# --------------------------------------------------------------------------

CLICKBAIT = re.compile(
    r"you won'?t believe|shocking|insane|mind[- ]?blow|gone wrong|must watch|"
    r"destroy(s|ed)\b|\bexposed\b|\bwtf\b|\bomg\b|this changes everything|"
    r"nobody (is )?talking about|(will|is going to) shock|before it'?s too late|"
    r"\bdon'?t (buy|watch)\b|\bend of\b.*\?|\bis (dead|over)\b|just (dropped|killed|changed)|"
    r"everyone (is |should |needs to )?(switching|know|use)|game[- ]?changer|you'?re (using|doing) .{0,30} wrong|"
    r"\b(rip|killer)\b|\w\.\.\s*$",
    re.I,
)
LIVE = re.compile(r"\b(live ?stream|livestream|live now|premiere|stream vod|"
                  r"live q ?& ?a|watch party)\b|🔴", re.I)
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]+")
KEEP_UPPER = {"AI", "ML", "LLM", "LLMs", "GPT", "GPU", "CPU", "TPU", "API", "MIT", "NASA",
              "USA", "US", "UK", "EU", "CEO", "CTO", "YC", "SQL", "HTML", "CSS", "JS", "C++",
              "OS", "RAM", "DNA", "RNA", "CERN", "IBM", "AMD", "TSMC", "ASML", "NVIDIA", "II",
              "III", "IV", "CS", "PhD", "RL", "RLHF", "AGI", "ASI", "NLP", "CNN", "RNN", "LSTM",
              "VC", "IPO", "M&A", "GDP", "FPGA", "ASIC", "x86", "ARM", "RISC-V", "TCP", "HTTP",
              "UDP", "DNS", "SSD", "JWST", "LHC", "QED", "QCD", "EV", "SpaceX", "IMO", "IOI", "IMC",
              "USAMO", "AIME", "AMC", "APMO", "INMO", "ISL", "ICPC", "CSES", "EGMO", "BMO", "PDE", "ODE"}


def caps_ratio(title: str, latin_only: bool = False) -> float:
    words = re.findall(r"[A-Za-z]{3,}" if latin_only else r"[A-Za-zА-Яа-яЁё]{3,}", title)
    if not words:
        return 0.0
    return sum(1 for w in words if w.isupper() and w not in KEEP_UPPER) / len(words)


def is_clickbait(title: str) -> bool:
    # Caps only count for Latin titles: some Russian lecturers title in capitals by style.
    return bool(CLICKBAIT.search(title)) or caps_ratio(title, latin_only=True) >= 0.5 or "!!" in title or "?!" in title


def calm_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", html.unescape(title))
    t = EMOJI.sub("", t)
    t = re.sub(r"(\s#[\w\u0400-\u04ff]+)+\s*$", "", t)  # trailing hashtags
    t = re.sub(r"([!?])\1+", r"\1", t)
    t = re.sub(r"\?!|!\?", "?", t)

    def fix(m: re.Match) -> str:
        w = m.group(0)
        if w in KEEP_UPPER or len(w) <= 3:
            return w
        return w.capitalize()

    if caps_ratio(t) >= 0.5:  # whole title shouted: sentence-case it, keep acronyms
        words = t.split(" ")
        keep = lambda w: re.split(r"[-/]", w.strip("()[]:,.!?\"'"))[0] in KEEP_UPPER or bool(re.search(r"\d", w))
        t = " ".join(w if keep(w) else w.lower() for w in words)
        t = t[:1].upper() + t[1:]
    t = re.sub(r"\b[A-ZА-ЯЁ][A-ZА-ЯЁ']{3,}\b", fix, t)
    return re.sub(r"\s{2,}", " ", t).strip(" -|·")


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------

def fetch_feed(channel_id: str, hl: str = "en") -> tuple[str, list[dict]]:
    """Latest uploads for a channel. Tries the RSS feed (channel, then uploads
    playlist); if YouTube refuses it (it 404s for some datacenter IPs), falls
    back to parsing the channel's /videos page."""
    errors = []
    for url in (f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{channel_id[2:]}"):
        try:
            return parse_rss(http_get(url, retries=1))
        except Exception as e:  # noqa: BLE001
            errors.append(f"rss: {e}")
    try:
        return scrape_videos_tab(channel_id, hl=hl)
    except Exception as e:  # noqa: BLE001
        errors.append(f"page: {e}")
    raise RuntimeError("; ".join(errors))


def parse_rss(xml: str) -> tuple[str, list[dict]]:
    root = ET.fromstring(xml)
    name = root.findtext("a:title", default="", namespaces=NS)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        if not vid:
            continue
        link = e.find("a:link", NS)
        href = link.get("href", "") if link is not None else ""
        stats = e.find("media:group/media:community/media:statistics", NS)
        views = int(stats.get("views", "-1")) if stats is not None else -1
        desc = e.findtext("media:group/media:description", default="", namespaces=NS) or ""
        out.append({
            "id": vid,
            "title": e.findtext("a:title", default="", namespaces=NS),
            "published": e.findtext("a:published", default="", namespaces=NS),
            "href": href,
            "views": views,
            "view_count": views if views > 0 else None,
            "desc": desc,
        })
    return name, out


# Handles both "3 weeks ago" and YouTube's compact "3w ago" / "9mo ago" / "1y ago".
_REL = re.compile(r"(\d+)\s*(seconds?|secs?|s|minutes?|mins?|mo|months?|m|hours?|hrs?|h|"
                  r"days?|d|weeks?|wks?|w|years?|yrs?|y)\b\.?\s*ago", re.I)


_REL_RU = re.compile(r"(\d+)\s*(секунд\w*|сек|минут\w*|мин|час\w*|ч|дн\w*|день|"
                     r"недел\w*|нед|месяц\w*|мес|год\w*|лет)\.?\s*назад", re.I)
_RU_DAYS = (("сек", 0), ("мин", 0), ("ч", 0), ("д", 1), ("нед", 7), ("мес", 30), ("год", 365), ("лет", 365))


def _unit_days(u: str) -> int:
    u = u.lower()
    for pre, days in _RU_DAYS:
        if u.startswith(pre):
            return days
    if u.startswith("mo"):
        return 30
    return {"s": 0, "m": 0, "h": 0, "d": 1, "w": 7, "y": 365}[u[0]]


PUB_SAMPLES: list = []


def rel_to_iso(text: str) -> str:
    m = _REL.search(text or "") or _REL_RU.search(text or "")
    if not m and len(PUB_SAMPLES) < 25:
        PUB_SAMPLES.append(text)  # unparsed date strings, surfaced in build_report.json
    days = int(m.group(1)) * _unit_days(m.group(2)) if m else 0
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _txt(node) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


def _walk(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            else:
                yield from _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, key)


def _initial_data(page: str) -> dict:
    m = re.search(r"ytInitialData\s*=\s*", page)
    if not m:
        raise ValueError("no ytInitialData")
    return json.JSONDecoder().raw_decode(page, m.end())[0]


_VIEWS = re.compile(r"([\d.,]+)\s*([KMB]?)\s*views?", re.I)
_VIEWS_RU = re.compile(r"(\d[\d\s\u00a0\u202f.,]*)\s*(тыс|млн|млрд)?\.?\s*просмотр", re.I)


def parse_views(text: str) -> int | None:
    """'1,234 views' / '12K views' / '1.2M views' -> int (None if absent)."""
    m = _VIEWS.search(text or "")
    if not m:
        r = _VIEWS_RU.search(text or "")
        if not r:
            return None
        num = re.sub(r"[\s\u00a0\u202f]", "", r.group(1)).replace(",", ".")
        try:
            return int(float(num) * {None: 1, "тыс": 1e3, "млн": 1e6, "млрд": 1e9}[(r.group(2) or "").lower() or None])
        except ValueError:
            return None
    num, suf = m.group(1), m.group(2).upper()
    try:
        val = float(num.replace(",", "")) if not suf else float(num.replace(",", "."))
    except ValueError:
        return None
    return int(val * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[suf])


def parse_duration(text: str) -> int | None:
    """'1:02:03' / '12:34' -> seconds."""
    if not text or not re.fullmatch(r"\d+(:\d{1,2}){1,2}", text.strip()):
        return None
    sec = 0
    for part in text.strip().split(":"):
        sec = sec * 60 + int(part)
    return sec


def _parse_video_items(node) -> list[dict]:
    """Pull video entries out of any chunk of ytInitialData / browse response."""
    out, seen = [], set()
    for r in _walk(node, "videoRenderer"):
        vid = r.get("videoId")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        pub = _txt(r.get("publishedTimeText"))
        overlays = json.dumps(r.get("thumbnailOverlays", [])) + json.dumps(r.get("badges", []))
        live_now = '"LIVE"' in overlays or "BADGE_STYLE_TYPE_LIVE_NOW" in overlays
        upcoming = "upcomingEventData" in r
        out.append({
            "id": vid,
            "title": _txt(r.get("title")),
            "published": rel_to_iso(pub),
            "href": f"https://www.youtube.com/watch?v={vid}",
            "views": 0 if (live_now or upcoming or not pub) else 1,
            "view_count": parse_views(_txt(r.get("viewCountText"))),
            "desc": _txt(r.get("descriptionSnippet")) or _txt(next(iter(_walk(r, "snippetText")), None)),
            "approx_date": True,
            "channel": _txt(r.get("ownerText") or r.get("longBylineText")),
            "channel_id": next((b.get("browseId") for b in _walk(r.get("ownerText") or r.get("longBylineText") or {},
                                                                 "browseEndpoint") if isinstance(b, dict)), None),
            "seconds": parse_duration(_txt(r.get("lengthText"))),
            "is_short": "/shorts/" in json.dumps(r.get("navigationEndpoint", {})),
        })
    for lv in _walk(node, "lockupViewModel"):  # newer layout
        vid = lv.get("contentId")
        if not vid or vid in seen or lv.get("contentType", "LOCKUP_CONTENT_TYPE_VIDEO") != "LOCKUP_CONTENT_TYPE_VIDEO":
            continue
        seen.add(vid)
        meta = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = meta.get("title", {}).get("content", "")
        parts = [p.get("text", {}).get("content", "") for p in _walk(meta, "metadataParts") for p in (p or [])]
        pub = next((p for p in parts if "ago" in p or "назад" in p), "")
        views = lockup_views(meta)
        secs = next((d for d in (parse_duration(b.get("text", "")) for b in _walk(lv.get("contentImage", {}), "thumbnailBadgeViewModel")
                                 if isinstance(b, dict)) if d), None)
        out.append({"id": vid, "title": title, "published": rel_to_iso(pub),
                    "href": f"https://www.youtube.com/watch?v={vid}", "views": 1 if pub else 0,
                    "view_count": views, "desc": "", "approx_date": True, "seconds": secs})
    return out


def lockup_views(meta) -> int | None:
    """View count from a lockup's metadata parts. The visible text is often just
    '168K' or '168 тыс.'; the word 'views' only appears in the accessibility label."""
    for parts in _walk(meta, "metadataParts"):
        for p in parts or []:
            text = (p.get("text") or {}).get("content", "")
            label = (p.get("accessibilityLabel") or "").lower()
            if "view" in text.lower() or "просмотр" in text.lower():
                return parse_views(text)
            if "просмотр" in label or re.search(r"[\u0400-\u04ff]", text):
                return parse_views(f"{text} просмотров")
            if "view" in label or "watching" not in label and (p.get("leadingIcon") or {}).get("name", "").startswith("PLAY_ARROW"):
                return parse_views(f"{text} views")
    return None


def _cont_token(node) -> str | None:
    """First continuationCommand token anywhere under node, including the newer
    nesting continuationCommand -> innertubeCommand -> continuationCommand."""
    for c in _walk(node, "continuationCommand"):
        if isinstance(c, dict):
            if c.get("token"):
                return c["token"]
            inner = _cont_token(c)
            if inner:
                return inner
    return None


def _continuation(node) -> str | None:
    """The paging token of a video list: the continuationItemRenderer that is
    the *last entry* of the grid (richGridRenderer.contents / gridRenderer.items)
    or of a continuation response's continuationItems. Tokens nested deeper
    (shelves, Home tab, menus) are never followed."""
    lists = ([g.get("contents") for g in _walk(node, "richGridRenderer")] +
             [g.get("items") for g in _walk(node, "gridRenderer")] +
             [g.get("contents") for g in _walk(node, "sectionListRenderer")] +
             [g.get("contents") for g in _walk(node, "playlistVideoListRenderer")] +
             list(_walk(node, "continuationItems")))
    for lst in lists:
        if isinstance(lst, list) and lst and isinstance(lst[-1], dict) and (
                "continuationItemRenderer" in lst[-1] or "continuationItemViewModel" in lst[-1]):
            tok = _cont_token(lst[-1])
            if tok:
                return tok
    return None


def http_post_json(url: str, payload: dict, retries: int = 3, hl: str = "en") -> dict:
    body = json.dumps(payload).encode()
    last: Exception | None = None
    for i in range(retries):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": UA, "Cookie": COOKIE, "Content-Type": "application/json",
            "Origin": "https://www.youtube.com", "Accept-Language": _accept_lang(hl)})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


ARCHIVE_DEBUG: list = []
VC_DEBUG: list = []
AVATARS: dict[str, str] = {}   # channel id -> avatar URL seen on its page
AVATAR_DIR = DATA / "avatars"
_VIDEOS_URL = re.compile(r"/videos/?(?:[?#].*)?$")


def _videos_tab(data: dict) -> dict | None:
    for tab in _walk(data, "tabRenderer"):
        ep = tab.get("endpoint", {})
        url = ep.get("commandMetadata", {}).get("webCommandMetadata", {}).get("url") or ""
        if _VIDEOS_URL.search(url) or tab.get("title") == "Videos":
            return tab
    return None


def scrape_videos_tab(channel_id: str, pages: int = 1, info: dict | None = None,
                      hl: str = "en") -> tuple[str, list[dict]]:
    """Parse https://www.youtube.com/channel/<id>/videos (Shorts live on a
    separate tab, so they never appear here). With pages > 1, keep scrolling
    the list through YouTube's public web 'browse' endpoint, the same request
    the site makes when you scroll; no API key of ours is involved.

    Paging only ever follows the Videos tab's own grid token. If that grid
    can't be located, we return the first page and report the archive as
    incomplete rather than following an unrelated token."""
    # hl = the channel's own language: with any other UI language YouTube
    # machine-translates titles (Russian lectures would come back in English).
    page = http_get(f"https://www.youtube.com/channel/{channel_id}/videos?hl={hl}&gl=US", hl=hl)
    data = _initial_data(page)
    name = (data.get("metadata", {}).get("channelMetadataRenderer", {}).get("title")
            or next(iter(_walk(data, "pageTitle")), ""))
    thumbs = data.get("metadata", {}).get("channelMetadataRenderer", {}).get("avatar", {}).get("thumbnails") or []
    if thumbs and thumbs[0].get("url"):
        AVATARS[channel_id] = thumbs[0]["url"]
    m = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', page)
    version = m.group(1) if m else "2.20251101.00.00"
    k = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
    endpoint = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false" + (f"&key={k.group(1)}" if k else "")
    ctx = {"client": {"clientName": "WEB", "clientVersion": version, "hl": hl, "gl": "US"}}
    dbg: dict = {"cid": channel_id, "client": version}

    grid = None  # the node we both parse items from and take the paging token from
    tab = _videos_tab(data)
    if tab and tab.get("selected") and tab.get("content"):
        grid, dbg["tab"] = tab["content"], "videos-tab"
    elif tab and pages > 1:
        # Only worth an extra request when we intend to page.
        params = tab.get("endpoint", {}).get("browseEndpoint", {}).get("params")
        if params:
            try:
                grid = http_post_json(endpoint, {"context": ctx, "browseId": channel_id, "params": params}, hl=hl)
                dbg["tab"] = "videos-tab-browsed"
            except Exception as e:  # noqa: BLE001
                dbg["tab_error"] = str(e)[:200]
    out = _parse_video_items(grid) if grid is not None else []
    pageable = bool(out)
    if out and not any(v.get("view_count") for v in out) and len(VC_DEBUG) < 3:
        raw = next(iter(_walk(grid, "videoRenderer")), None) or next(iter(_walk(grid, "lockupViewModel")), None)
        VC_DEBUG.append(channel_id)
        meta = (raw or {}).get("metadata", raw)
        print(f"  [no view counts] {channel_id}: " + json.dumps(meta, ensure_ascii=False)[:3000])
    if not out:
        # Whole-page fallback: fine for "latest uploads", never used for paging.
        out = _parse_video_items(data)
        dbg["tab"] = dbg.get("tab", "none") + "+whole-page"
    if not out:
        raise ValueError("no videos found on page")
    seen = {v["id"] for v in out}

    token = _continuation(grid) if pageable else None
    dbg.update(first_page=len(out), token=bool(token))
    if pageable and not token and len(ARCHIVE_DEBUG) < 4:
        items = next(iter(_walk(grid, "contents")), None)
        dbg["grid_tail_keys"] = [list(x)[:3] for x in items[-3:]] if isinstance(items, list) else repr(type(items))
    fetched = 1
    while token and fetched < pages and len(out) < MAX_PER_CHANNEL:
        time.sleep(0.4)  # be gentle
        try:
            resp = http_post_json(endpoint, {"context": ctx, "continuation": token}, hl=hl)
        except Exception as e:  # noqa: BLE001
            dbg["post_error"] = str(e)[:300]
            break
        items = [v for v in _parse_video_items(resp) if v["id"] not in seen]
        fetched += 1
        if not items:
            dbg["empty_response_keys"] = list(resp)[:10]
            break
        seen.update(v["id"] for v in items)
        out.extend(items)
        token = _continuation(resp)
    dbg.update(pages=fetched, videos=len(out))
    if len(ARCHIVE_DEBUG) < 6:
        ARCHIVE_DEBUG.append(dbg)
    if info is not None:
        # Complete = the list really ended (or we hit the cap), not a dead end.
        clean = not any(k in dbg for k in ("post_error", "empty_response_keys", "tab_error"))
        info["complete"] = pageable and clean and (len(out) >= MAX_PER_CHANNEL or token is None)
    return name, out[:MAX_PER_CHANNEL]


SEARCH_SORT = {"date": "CAISAhAB", "relevance": "EgIQAQ=="}   # videos only


def fetch_search(query: str, hl: str = "en", pages: int = 2, sort: str = "date") -> list[dict]:
    """YouTube search results (videos only), paged like the site's own scroll."""
    q = urllib.parse.urlencode({"search_query": query, "sp": SEARCH_SORT[sort], "hl": hl, "gl": "US"})
    page = http_get(f"https://www.youtube.com/results?{q}", hl=hl)
    data = _initial_data(page)
    m = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', page)
    version = m.group(1) if m else "2.20251101.00.00"
    out = _parse_video_items(data)
    seen = {v["id"] for v in out}
    token, fetched = _continuation(data), 1
    while token and fetched < pages:
        time.sleep(0.4)
        try:
            resp = http_post_json("https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                                  {"context": {"client": {"clientName": "WEB", "clientVersion": version,
                                                          "hl": hl, "gl": "US"}}, "continuation": token}, hl=hl)
        except Exception:  # noqa: BLE001
            break
        items = [v for v in _parse_video_items(resp) if v["id"] not in seen]
        fetched += 1
        if not items:
            break
        seen.update(v["id"] for v in items)
        out.extend(items)
        token = _continuation(resp)
    return out


CC_TITLE = re.compile(r"claude[\s_-]?code|клод\s*код", re.I)
_NON_LATIN_OTHER = re.compile(r"[\u0370-\u03ff\u0590-\u06ff\u0900-\u0dff\u0e00-\u0e7f\u3040-\u30ff"
                              r"\u3400-\u9fff\uac00-\ud7af]")
_NOT_EN = {"para", "con", "como", "usar", "el", "los", "las", "una", "uno", "del", "que", "em", "com",
           "você", "não", "und", "mit", "für", "der", "die", "das", "ist", "avec", "pour", "les",
           "des", "est", "une", "dans", "ein", "eine", "wie", "sur", "pra", "seu", "sua"}


def video_lang(title: str, desc: str = "", channel: str = "") -> str | None:
    """'ru', 'en' or None (other language). The channel name counts too:
    YouTube auto-translates titles, so a Japanese video can arrive with an
    English title, but its channel name gives it away."""
    if _NON_LATIN_OTHER.search(channel or ""):
        return None
    text = f"{title} {desc}"
    if re.search(r"[\u0400-\u04ff]", title):
        return "ru"
    if _NON_LATIN_OTHER.search(text) or re.search(r"[ñãõç¿¡ßü]", text.lower()):
        return None
    words = set(re.findall(r"[a-zà-ÿ]+", text.lower()))
    return None if words & _NOT_EN else "en"


def claude_code_videos(cfg: dict, deep: bool) -> tuple[list[dict], dict]:
    """Run the configured searches; return filtered videos + stats."""
    stats = {"raw": 0, "kept": 0, "by_lang": {"en": 0, "ru": 0}, "dropped": {}}
    found: dict[str, dict] = {}
    jobs = []
    for qd in cfg.get("queries") or []:
        jobs.append((qd["q"], qd.get("hl", "en"), cfg.get("pages", 2), "date"))
        if deep:
            jobs.append((qd["q"], qd.get("hl", "en"), cfg.get("backfill_pages", 15), "date"))
            jobs.append((qd["q"], qd.get("hl", "en"), cfg.get("backfill_pages", 15), "relevance"))
    min_sec = int(cfg.get("min_minutes", 2) * 60)

    def drop(why):
        stats["dropped"][why] = stats["dropped"].get(why, 0) + 1

    with cf.ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda j: (j, _safe(lambda: fetch_search(j[0], j[1], j[2], j[3]))), jobs))
    for (query, hl, *_), items in results:
        if isinstance(items, Exception):
            stats.setdefault("errors", []).append(f"{query}/{hl}: {items}")
            continue
        for v in items:
            stats["raw"] += 1
            if v["id"] in found:
                continue
            if not CC_TITLE.search(v["title"]):
                drop("title"); continue
            if v.get("is_short") or not v.get("seconds") or v["seconds"] < min_sec:
                drop("short/no-duration"); continue
            if LIVE.search(v["title"]) or not v.get("views"):
                drop("live"); continue
            lang = video_lang(v["title"], v.get("desc", ""), v.get("channel", ""))
            if lang not in ("en", "ru"):
                drop("language"); continue
            found[v["id"]] = {**v, "lang": lang}
    stats["kept"] = len(found)
    for v in found.values():
        stats["by_lang"][v["lang"]] += 1
    return list(found.values()), stats


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return e


def snippet(desc: str) -> str:
    # Drop links, hashtags, timestamps and sponsor boilerplate; keep the gist.
    lines = []
    for ln in desc.splitlines():
        ln = ln.strip()
        if not ln or re.search(r"https?://|sponsor|patreon|subscribe|merch|discount|promo code|"
                               r"^\d{1,2}:\d{2}|#\w+", ln, re.I):
            continue
        lines.append(ln)
    s = " ".join(lines)
    return (s[: DESC_CHARS - 1] + "…") if len(s) > DESC_CHARS else s


def oembed(video_id: str) -> dict | None:
    u = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        f"https://www.youtube.com/watch?v={video_id}")
    try:
        return json.loads(http_get(u, retries=2))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def channel_lang(c: dict, key: str) -> str:
    """UI language to request a channel in: `lang:` from channels.yml, else
    Russian for Cyrillic names, else English."""
    if c.get("lang"):
        return c["lang"]
    return "ru" if re.search(r"[\u0400-\u04FF]", c.get("name") or key) else "en"


def round_sig(n: int, sig: int = 2) -> int:
    """12,345 -> 12,000: view counts barely matter at finer grain, and rounding
    keeps hourly runs from committing a new library just because views ticked up."""
    if n <= 0:
        return n
    from math import floor, log10
    return int(round(n, -int(floor(log10(n))) + (sig - 1)))


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


# --------------------------------------------------------------------------
# Channel playlists (course-like ones only)
# --------------------------------------------------------------------------

PLAYLISTS_JSON = DATA / "playlists.json"
PL_DEBUG: list = []
PL_VERSION = 5   # bump when playlist parsing changes, to re-fetch every playlist
_PL_COUNT = re.compile(r"(\d[\d,.\s ]*)\s*(videos?|видео|lessons?|уроков|урока|урок|տեսանյութ)", re.I)
_NUMBERED = re.compile(
    r"(?:(?<!\w)(?:lecture|lec|lesson|part|episode|ep|class|chapter|session|module|unit|day|week|"
    r"problem|exercise|лекция|занятие|урок|семинар|часть|выпуск|серия|день|неделя|задача|задание|դաս)\.?\s*(?:#|№)?\s*\d{1,3}(?!\d)"
    r"|[#№]\s?\d{1,3}(?!\d)|^\s*\[?\d{1,3}[.)\]]\s)", re.I)
_UNAVAILABLE = re.compile(r"^\[(private|deleted|unavailable)[^\]]*\]$|^\[(частное|удал[её]нное)[^\]]*\]$", re.I)


def _pl_count(text: str) -> int | None:
    m = _PL_COUNT.search(text or "")
    if not m:
        return None
    try:
        return int(re.sub(r"[\s,. ]", "", m.group(1)))
    except ValueError:
        return None


def _parse_playlist_cards(node) -> list[dict]:
    """Playlists on a channel's Playlists tab (both page layouts)."""
    out, seen = [], set()
    for lv in _walk(node, "lockupViewModel"):
        if lv.get("contentType") != "LOCKUP_CONTENT_TYPE_PLAYLIST":
            continue
        pid = lv.get("contentId")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = lv.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {}).get("content", "")
        badges = [b.get("text", "") for b in _walk(lv.get("contentImage", {}), "thumbnailBadgeViewModel") if isinstance(b, dict)]
        count = next((c for c in (_pl_count(t) for t in badges) if c is not None), None)
        out.append({"id": pid, "title": title, "count": count})
    for g in _walk(node, "gridPlaylistRenderer"):
        pid = g.get("playlistId")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        count = _pl_count(_txt(g.get("videoCountText"))) or _pl_count(_txt(g.get("videoCountShortText")))
        out.append({"id": pid, "title": _txt(g.get("title")), "count": count})
    return out


def _parse_playlist_rows(node) -> list[dict]:
    """Videos on a playlist page: older playlistVideoRenderer rows, or the newer
    video lockups (same shape as on channel pages)."""
    out = []
    seen = set()
    for r in _walk(node, "playlistVideoRenderer"):
        vid = r.get("videoId")
        title = _txt(r.get("title"))
        if not vid or _UNAVAILABLE.match(title.strip()) or r.get("isPlayable") is False:
            continue
        secs = r.get("lengthSeconds")
        seen.add(vid)
        out.append({"id": vid, "title": title,
                    "seconds": int(secs) if str(secs or "").isdigit() else parse_duration(_txt(r.get("lengthText"))),
                    "channel": _txt(r.get("shortBylineText"))})
    for lv in _walk(node, "lockupViewModel"):
        if lv.get("contentType", "LOCKUP_CONTENT_TYPE_VIDEO") != "LOCKUP_CONTENT_TYPE_VIDEO":
            continue
        vid = lv.get("contentId")
        meta = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = meta.get("title", {}).get("content", "")
        if not vid or vid in seen or _UNAVAILABLE.match(title.strip()):
            continue
        seen.add(vid)
        secs = next((d for d in (parse_duration(b.get("text", "")) for b in _walk(lv.get("contentImage", {}), "thumbnailBadgeViewModel")
                                 if isinstance(b, dict)) if d), None)
        parts = [q.get("text", {}).get("content", "") for p in _walk(meta, "metadataParts") for q in (p or [])]
        channel = next((t for t in parts if t and not re.search(r"\d", t)), "")
        out.append({"id": vid, "title": title, "seconds": secs, "channel": channel})
    return out


def _browse_pages(page: str, first: dict, parse, hl: str, max_items: int) -> list[dict]:
    """Parse a page's items, then follow its list continuation until max_items."""
    m = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', page)
    k = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
    endpoint = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false" + (f"&key={k.group(1)}" if k else "")
    ctx = {"client": {"clientName": "WEB", "clientVersion": m.group(1) if m else "2.20251101.00.00", "hl": hl, "gl": "US"}}
    out = parse(first)
    seen = {x["id"] for x in out}
    token = _continuation(first)
    if not token and parse is _parse_playlist_rows:
        # Newer playlist pages nest the list differently; on a playlist page the
        # only continuation is the list's own, so take it wherever it sits.
        token = next((t for key in ("continuationItemRenderer", "continuationItemViewModel") for cir in _walk(first, key)
                      for t in [_cont_token(cir)] if t), None)
    if not token and parse is _parse_playlist_rows and len(out) >= 100 and len(PL_DEBUG) < 8:
        PL_DEBUG.append("cont")
        raw = json.dumps(first, ensure_ascii=False)
        hits = [raw[max(0, m.start() - 400): m.start() + 300] for m in re.finditer(r"ontinuation", raw)][:4]
        print(f"  [playlist 100+ without token] samples: " + " ||| ".join(hits)[:3000])
    while token and len(out) < max_items:
        time.sleep(0.3)
        try:
            resp = http_post_json(endpoint, {"context": ctx, "continuation": token}, hl=hl)
        except Exception as e:  # noqa: BLE001
            if len(PL_DEBUG) < 10:
                PL_DEBUG.append("post")
                print(f"  [playlist next page failed] {e}")
            break
        items = [x for x in parse(resp) if x["id"] not in seen]
        if not items:
            if parse is _parse_playlist_rows and len(PL_DEBUG) < 10:
                PL_DEBUG.append("empty")
                raw = json.dumps(resp, ensure_ascii=False)
                print(f"  [playlist next page empty] keys={list(resp)[:8]} sample=" + raw[:1500] + " ... " + raw[-1200:])
            break
        seen.update(x["id"] for x in items)
        out.extend(items)
        token = _continuation(resp)
        if not token and parse is _parse_playlist_rows:
            token = next((t for key in ("continuationItemRenderer", "continuationItemViewModel") for cir in _walk(resp, key)
                          for t in [_cont_token(cir)] if t), None)
    return out[:max_items]


def fetch_channel_playlists(cid: str, hl: str, max_lists: int) -> list[dict]:
    page = http_get(f"https://www.youtube.com/channel/{cid}/playlists?view=1&sort=lad&hl={hl}&gl=US", hl=hl)
    data = _initial_data(page)
    out = _browse_pages(page, data, _parse_playlist_cards, hl, max_lists)
    if not out and len(PL_DEBUG) < 3 and "playlist" in page.lower():
        PL_DEBUG.append(cid)
        tab = next((t for t in _walk(data, "tabRenderer") if t.get("selected")), None)
        print(f"  [no playlists parsed] {cid}: " + json.dumps(tab or {}, ensure_ascii=False)[:2500])
    return out


def fetch_playlist_items(pid: str, hl: str, max_items: int) -> list[dict]:
    page = http_get(f"https://www.youtube.com/playlist?list={pid}&hl={hl}&gl=US", hl=hl)
    data = _initial_data(page)
    out = _browse_pages(page, data, _parse_playlist_rows, hl, max_items)
    if not out and len(PL_DEBUG) < 6:
        PL_DEBUG.append(pid)
        raw = next(iter(_walk(data, "playlistVideoListRenderer")), None) or \
            next(iter(_walk(data, "lockupViewModel")), None) or {"keys": list(data)[:12]}
        print(f"  [no playlist items parsed] {pid}: " + json.dumps(raw, ensure_ascii=False)[:2500])
    return out


def is_course_like(title: str, items: list[dict], cfg: dict, channel_name: str = "") -> bool:
    if len(items) < int(cfg.get("min_videos", 3)):
        return False
    if all((v.get("seconds") or 999) <= 60 for v in items):
        return False                                   # Shorts-only playlist
    if sum(1 for v in items if LIVE.search(v["title"])) > len(items) / 2:
        return False                                   # mostly live streams
    if channel_name and _norm(title) == _norm(channel_name):
        return False                                   # catch-all "all my uploads" playlist
    words = cfg.get("keywords") or []
    # English keywords match whole words ("class" not "classic"); others are stems ("лекци" -> лекция/лекции).
    if any(re.search(r"(?<!\w)" + re.escape(w) + (r"s?(?!\w)" if w.isascii() else ""), title, re.I) for w in words):
        return True
    numbered = sum(1 for v in items if _NUMBERED.search(v["title"]))
    return numbered >= len(items) / 2


def update_playlists(cfg: dict, chans: list[dict], library: dict, workers: int, offline: bool) -> dict:
    """Refresh course-like playlists of your channels into site/data/playlists.json.
    Gentle: a channel's playlist list at most daily, a playlist's items only when
    its count changes (or weekly), and at most `per_run` channels per run."""
    pcfg = cfg.get("playlists") or {}
    prev = load_json(PLAYLISTS_JSON, {})
    state = prev.get("state") or {"ch": {}, "pl": {}}
    if state.get("v") != PL_VERSION:          # parser changed: fetch everything again
        state = {"v": PL_VERSION, "ch": {}, "pl": {}}
    state.setdefault("ch", {}); state.setdefault("pl", {})
    keep: dict[str, dict] = {p["id"]: p for p in prev.get("playlists", [])}
    extra: dict[str, dict] = dict(prev.get("extra") or {})
    stats = {"channels_checked": 0, "lists_seen": 0, "lists_fetched": 0, "kept": 0, "skipped": 0, "errors": []}
    now = time.time()
    day, week = 86400, 7 * 86400
    keys = {c["key"] for c in chans if c.get("cid")}
    keep = {k: p for k, p in keep.items() if p["ch"] in keys}          # channel removed -> its playlists go
    if not offline and pcfg.get("enabled", True):
        due = sorted((c for c in chans if c.get("cid") and now - state["ch"].get(c["key"], 0) > day),
                     key=lambda c: state["ch"].get(c["key"], 0))[: int(pcfg.get("per_run", 25))]

        def one(c):
            res = {"key": c["key"], "lists": [], "err": None}
            try:
                cards = fetch_channel_playlists(c["cid"], c["hl"], int(pcfg.get("max_per_channel", 60)))
                for card in cards:
                    st = state["pl"].get(card["id"], {})
                    fresh = (card.get("count") is not None and card["count"] == st.get("count")
                             and now - st.get("at", 0) < week)
                    if fresh:
                        res["lists"].append((card, None))
                        continue
                    items = fetch_playlist_items(card["id"], c["hl"], int(pcfg.get("max_items", 300)))
                    res["lists"].append((card, items))
            except Exception as e:  # noqa: BLE001
                res["err"] = f"{c['key']}: {e}"
            return c, res

        with cf.ThreadPoolExecutor(min(workers, 6)) as ex:
            for c, res in ex.map(one, due):
                stats["channels_checked"] += 1
                if res["err"]:
                    stats["errors"].append(res["err"][:200])
                    continue
                state["ch"][c["key"]] = now
                seen_ids = set()
                for card, items in res["lists"]:
                    stats["lists_seen"] += 1
                    seen_ids.add(card["id"])
                    if items is None:
                        continue                                   # unchanged since last fetch
                    stats["lists_fetched"] += 1
                    state["pl"][card["id"]] = {"count": card.get("count"), "at": now}
                    title = calm_title(card["title"])
                    if is_course_like(title, items, pcfg, c.get("name") or c["key"]):
                        keep[card["id"]] = {"id": card["id"], "t": title, "ch": c["key"], "ids": [v["id"] for v in items]}
                        for v in items:
                            if v["id"] not in library:
                                extra[v["id"]] = {"t": calm_title(v["title"]), "chn": v.get("channel") or "",
                                                  **({"s": v["seconds"]} if v.get("seconds") else {})}
                        stats["kept"] += 1
                    else:
                        keep.pop(card["id"], None)
                        stats["skipped"] += 1
                # playlists the channel deleted
                for pid in [p for p, x in keep.items() if x["ch"] == c["key"] and p not in seen_ids]:
                    keep.pop(pid)
    used = {i for p in keep.values() for i in p["ids"]}
    extra = {k: v for k, v in extra.items() if k in used and k not in library}
    for p in keep.values():
        p["n"] = len(p["ids"])
    out = {"playlists": sorted(keep.values(), key=lambda p: (p["ch"], p["t"])), "extra": extra, "state": state}
    if json.dumps(out, sort_keys=True) != json.dumps({k: prev.get(k) for k in out}, sort_keys=True):
        PLAYLISTS_JSON.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    stats["total"] = len(keep)
    print(f"  playlists: {stats['total']} course-like kept (checked {stats['channels_checked']} channels, "
          f"fetched {stats['lists_fetched']} of {stats['lists_seen']} lists, skipped {stats['skipped']})")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="no network; reuse existing data")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-archives", type=int, default=None,
                    help="limit how many channels get their full archive fetched this run")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "channels.yml").read_text())
    classics_cfg = yaml.safe_load((ROOT / "classics.yml").read_text()) or {}
    topics: dict[str, str] = cfg["topics"]
    channels_cfg: list[dict] = cfg.get("channels") or []

    DATA.mkdir(parents=True, exist_ok=True)
    prev = load_json(VIDEOS_JSON, {})
    prev_ch = {c["key"]: c for c in prev.get("channels", [])}
    library: dict[str, dict] = {}
    for v in prev.get("videos", []):
        library[v["id"]] = v
    ids_cache: dict[str, str] = load_json(IDS_JSON, {})
    report = {"resolved": [], "unresolved": [], "feed_errors": [], "dropped": {"short": 0, "live": 0,
              "filtered": 0}, "new": 0}

    # ---- resolve channel ids -------------------------------------------------
    chans = []
    for c in channels_cfg:
        key = (c.get("handle") or c.get("channel_id") or c.get("name") or "").lstrip("@")
        if not key:
            continue
        if c.get("topic") not in topics:
            print(f"! {key}: unknown topic {c.get('topic')!r}", file=sys.stderr)
            continue
        cid = c.get("channel_id") or ids_cache.get(key.lower())
        chans.append({**c, "key": key, "cid": cid, "hl": channel_lang(c, key)})

    if not args.offline:
        todo = [c for c in chans if not c["cid"]]
        def res(c):
            if c.get("handle"):
                cid = resolve_handle(c["key"])
                return (cid, "@" + c["key"]) if cid else None
            return resolve_name(c["key"])

        with cf.ThreadPoolExecutor(args.workers) as ex:
            for c, hit in zip(todo, ex.map(res, todo)):
                if hit:
                    cid, matched = hit
                    c["cid"] = cid
                    ids_cache[c["key"].lower()] = cid
                    exact = c.get("handle") or _norm(matched) == _norm(c["key"])
                    report["resolved"].append({"entry": c["key"], "matched": matched, "id": cid,
                                               **({} if exact else {"check": True})})
                    print(f"  resolved {c['key']!r} -> {matched!r} {cid}{'' if exact else '  <- CHECK'}")
                else:
                    report["unresolved"].append(c["key"])
                    print(f"! could not resolve @{c['key']}", file=sys.stderr)

    # ---- fetch feeds ---------------------------------------------------------
    channel_meta: dict[str, dict] = {}
    for c in chans:
        channel_meta[c["key"]] = {"key": c["key"], "name": prev_ch.get(c["key"], {}).get("name", c["key"]),
                                  "topic": c["topic"], "cid": c.get("cid"), "sub": c.get("sub")}

    feeds: dict[str, tuple[str, list[dict]]] = {}
    # backfill.json: {channel key: {"cid": ..., "cap": cap used, "n": videos fetched}}
    # (legacy entries were a bare channel-id string, fetched under a cap of 300).
    have = collections.Counter(v.get("ch") for v in library.values())
    backfill: dict[str, dict] = {k: (v if isinstance(v, dict) else {"cid": v, "cap": 300, "n": 300 if have.get(k, 0) >= 270 else have.get(k, 0)})
                                 for k, v in load_json(BACKFILL_JSON, {}).items()}

    tot_ch = collections.Counter(v.get("ch") for v in library.values())
    vc_ch = collections.Counter(v.get("ch") for v in library.values() if v.get("vc"))
    vc_cover = {k: vc_ch[k] / n for k, n in tot_ch.items() if n}
    dur_ch = collections.Counter(v.get("ch") for v in library.values() if v.get("s"))
    dur_cover = {k: dur_ch[k] / n for k, n in tot_ch.items() if n}

    def needs_archive(c) -> bool:
        b = backfill.get(c["key"])
        if not b or b.get("cid") != c["cid"] or b.get("hl", "en") != c["hl"]:
            return True
        if c["hl"] != "en" and b.get("tv", 1) < TITLES_VERSION:  # fetched before titles were fixed
            return True
        # Archived before durations were recorded: fetch once more to fill them in.
        if b.get("dv", 0) < DUR_VERSION and dur_cover.get(c["key"], 0) < 0.5:
            return True
        # Archived before view counts were recorded: fetch once more to fill them in.
        if b.get("vv", 0) < VIEWS_VERSION and vc_cover.get(c["key"], 0) < 0.5:
            return True
        # Re-fetch channels that were cut off by a smaller cap than today's.
        return b.get("cap", 0) < MAX_PER_CHANNEL and b.get("n", 0) >= b.get("cap", 0)

    archive_pages = max(1, -(-MAX_PER_CHANNEL // 30))  # ~30 videos per page
    report["archived"] = []
    if not args.offline:
        live = [c for c in chans if c.get("cid")]
        todo_archive = {c["key"] for c in live if needs_archive(c)}
        if args.max_archives is not None:
            todo_archive = set(sorted(todo_archive)[: args.max_archives])

        def grab(c):
            try:
                if c["key"] in todo_archive:
                    try:
                        info: dict = {}
                        res = scrape_videos_tab(c["cid"], pages=archive_pages, info=info, hl=c["hl"])
                        return c["key"], res, info.get("complete", False)
                    except Exception:  # noqa: BLE001  fall back to the normal path
                        pass
                return c["key"], fetch_feed(c["cid"], c["hl"]), False
            except Exception as e:  # noqa: BLE001
                return c["key"], e, False

        with cf.ThreadPoolExecutor(args.workers) as ex:
            for c_, (key, res, archived) in zip(live, ex.map(grab, live)):
                if archived:
                    backfill[key] = {"cid": c_["cid"], "cap": MAX_PER_CHANNEL, "n": len(res[1]), "hl": c_["hl"], "tv": TITLES_VERSION, "vv": VIEWS_VERSION, "dv": DUR_VERSION}
                    report["archived"].append(f"{key}: {len(res[1])}")
                    print(f"  archived {key}: {len(res[1])} videos")
                if isinstance(res, Exception):
                    report["feed_errors"].append(f"{key}: {res}")
                    print(f"! feed {key}: {res}", file=sys.stderr)
                else:
                    feeds[key] = res

    # ---- Claude Code: YouTube search, any channel ---------------------------
    cc_cfg = cfg.get("claude_code") or {}
    cc_items: list[dict] = []
    if cc_cfg and not args.offline:
        deep = (backfill.get("__claude_code__") or {}).get("v") != CC_VERSION
        cc_items, cc_stats = claude_code_videos(cc_cfg, deep)
        report["claude_code"] = {**cc_stats, "deep": deep, "new": 0}
        print(f"  claude code: {cc_stats['kept']} kept of {cc_stats['raw']} ({cc_stats['by_lang']}), deep={deep}")
        if deep and cc_stats["kept"] and not cc_stats.get("errors"):
            backfill["__claude_code__"] = {"v": CC_VERSION}

    # ---- channel avatars (so the Channels page shows who each one really is) --
    if not args.offline:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)

        def avatar_url(cid):
            if cid in AVATARS:
                return cid, AVATARS[cid]
            try:
                d = _initial_data(http_get(f"https://www.youtube.com/channel/{cid}?hl=en", retries=2))
                th = d.get("metadata", {}).get("channelMetadataRenderer", {}).get("avatar", {}).get("thumbnails") or []
                return cid, th[0]["url"] if th else None
            except Exception:  # noqa: BLE001
                return cid, None

        need = [c["cid"] for c in chans if c.get("cid") and not (AVATAR_DIR / f"{c['cid']}.jpg").exists()]
        with cf.ThreadPoolExecutor(args.workers) as ex:
            found = {cid: url for cid, url in ex.map(avatar_url, need) if url}
        for cid, url in found.items():
            f = AVATAR_DIR / f"{cid}.jpg"
            if f.exists():
                continue
            try:
                req = urllib.request.Request(re.sub(r"=s\d+", "=s88", url), headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    f.write_bytes(r.read())
            except Exception as e:  # noqa: BLE001
                print(f"! avatar {cid}: {e}", file=sys.stderr)

    # ---- filter + merge ------------------------------------------------------
    candidates = []
    for c in chans:
        if c["key"] not in feeds:
            continue
        name, entries = feeds[c["key"]]
        if name:
            channel_meta[c["key"]]["name"] = name
        inc = [w.lower() for w in c.get("include", [])]
        exc = [w.lower() for w in c.get("exclude", [])]
        for e in entries:
            tl = e["title"].lower()
            if inc and not any(w in tl for w in inc):
                report["dropped"]["filtered"] += 1
                continue
            if exc and any(w in tl for w in exc):
                report["dropped"]["filtered"] += 1
                continue
            if "/shorts/" in e["href"] or "#shorts" in tl:
                report["dropped"]["short"] += 1
                library.pop(e["id"], None)
                continue
            if e.get("seconds") and e["seconds"] <= 60:   # a Short posted as a regular video
                report["dropped"]["short"] += 1
                library.pop(e["id"], None)
                continue
            if LIVE.search(e["title"]) or e["views"] == 0:
                # 0 views == upcoming premiere / scheduled stream placeholder.
                report["dropped"]["live"] += 1
                continue
            candidates.append((c, e))

    # Shorts don't always say so in the feed: probe new videos once.
    new = [(c, e) for c, e in candidates if e["id"] not in library and not e.get("approx_date")]
    if new and not args.offline:
        with cf.ThreadPoolExecutor(args.workers) as ex:
            flags = list(ex.map(lambda ce: is_short(ce[1]["id"]), new))
        short_ids = {ce[1]["id"] for ce, f in zip(new, flags) if f}
    else:
        short_ids = set()

    for c, e in candidates:
        if e["id"] in short_ids:
            report["dropped"]["short"] += 1
            continue
        is_new = e["id"] not in library
        library[e["id"]] = {
            **library.get(e["id"], {}),
            "id": e["id"],
            "t": calm_title(e["title"]),
            "ch": c["key"],
            "tp": c["topic"],
            "p": (library.get(e["id"], {}).get("p") if e.get("approx_date") and library.get(e["id"], {}).get("p")
                  else e["published"][:10]),
            "d": snippet(e["desc"]),
            **({"vc": round_sig(e["view_count"])} if e.get("view_count") else {}),
            **({"s": e["seconds"]} if e.get("seconds") else {}),
            **({"cb": 1} if is_clickbait(e["title"]) else {}),
        }
        if not is_clickbait(e["title"]):
            library[e["id"]].pop("cb", None)
        report["new"] += int(is_new)

    # ---- merge Claude Code results -------------------------------------------
    cc_topic = cc_cfg.get("topic", "claude")
    for e in cc_items:
        cur = library.get(e["id"], {})
        entry = {**cur, "id": e["id"], "cc": 1, "lang": e["lang"]}
        if not cur.get("ch"):  # not already in the library via one of your channels
            entry.update(t=calm_title(e["title"]), ch="", tp=cc_topic,
                         chn=e.get("channel") or cur.get("chn", ""),
                         ccid=e.get("channel_id") or cur.get("ccid"),
                         p=cur.get("p") or e["published"][:10],
                         d=snippet(e.get("desc") or "") or cur.get("d", ""))
            if e.get("view_count"):
                entry["vc"] = round_sig(e["view_count"])
            if e.get("seconds"):
                entry["s"] = e["seconds"]
            if is_clickbait(e["title"]):
                entry["cb"] = 1
            else:
                entry.pop("cb", None)
        report["claude_code"]["new"] += int(e["id"] not in library)
        library[e["id"]] = entry

    # ---- classics ------------------------------------------------------------
    for v in classics_cfg.get("videos") or []:
        vid = v["id"]
        cur = library.get(vid, {})
        title = v.get("title") or cur.get("t")
        chname = v.get("channel") or cur.get("chn")
        if (not title or not chname) and not args.offline:
            meta = oembed(vid) or {}
            title = title or meta.get("title")
            chname = chname or meta.get("author_name")
        if not title:
            print(f"! classic {vid}: no title (offline or unavailable)", file=sys.stderr)
            continue
        library[vid] = {**cur, "id": vid, "t": calm_title(title), "tp": v.get("topic", cur.get("tp", "ai")),
                        "ch": cur.get("ch", ""), "chn": chname or "", "p": cur.get("p", ""),
                        "d": cur.get("d", ""), "ev": 1}

    # ---- drop videos from channels you removed; cap per channel --------------
    keep_keys = {c["key"] for c in chans}
    by_ch: dict[str, list[dict]] = {}
    for v in library.values():
        if v.get("cc") and not v.get("ch") and _NON_LATIN_OTHER.search(v.get("chn", "")):
            continue  # search-found video from a channel in another language
        if v.get("s") and v["s"] <= 60 and not v.get("ev"):
            continue  # Short-length clip stored before durations were known
        if v.get("ev") or v.get("ch") in keep_keys or (v.get("cc") and not v.get("ch")):
            by_ch.setdefault(v.get("ch") or ("_ev" if v.get("ev") else "_cc"), []).append(v)
    videos = []
    for k, vs in by_ch.items():
        vs.sort(key=lambda v: v.get("p", ""), reverse=True)
        if k == "_cc":
            videos.extend(vs[: int(cc_cfg.get("max_videos", 2000))])
            continue
        videos.extend(vs if k == "_ev" else [v for v in vs[:MAX_PER_CHANNEL]] +
                      [v for v in vs[MAX_PER_CHANNEL:] if v.get("ev")])
    # Videos follow their channel's current topic (so moving a channel in
    # channels.yml re-files its whole history).
    topic_of = {c["key"]: c["topic"] for c in chans}
    for v in videos:
        if not v.get("ev") and v.get("ch") in topic_of:
            v["tp"] = topic_of[v["ch"]]
    # topic sanity
    for v in videos:
        if v["tp"] not in topics:
            v["tp"] = next(iter(topics))
    videos.sort(key=lambda v: (v.get("p", ""), v["id"]), reverse=True)

    used = {v.get("ch") for v in videos}
    out = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "topics": [{"key": k, "name": n, **({"subs": cfg["subtopics"][k]} if k in (cfg.get("subtopics") or {}) else {})}
                   for k, n in topics.items()],
        "channels": [{"key": m["key"], "name": m["name"], "topic": m["topic"], "cid": m.get("cid"),
                      **({"sub": m["sub"]} if m.get("sub") else {}),
                      **({"av": 1} if m.get("cid") and (AVATAR_DIR / f"{m['cid']}.jpg").exists() else {})}
                     for m in channel_meta.values() if m["key"] in used],
        "videos": videos,
    }
    # Keep the previous timestamp when nothing else changed, so an hourly run
    # with no new videos produces no commit.
    if prev and {k: v for k, v in prev.items() if k != "generated"} == {k: v for k, v in out.items() if k != "generated"}:
        out["generated"] = prev.get("generated", out["generated"])
        report["unchanged"] = True
    VIDEOS_JSON.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    IDS_JSON.write_text(json.dumps(dict(sorted(ids_cache.items())), indent=1))
    if not args.offline:
        BACKFILL_JSON.write_text(json.dumps(dict(sorted(backfill.items())), indent=1, ensure_ascii=False))
    report["playlists"] = update_playlists(cfg, chans, {v["id"]: v for v in videos}, args.workers, args.offline)
    report["total"] = len(videos)
    report["pub_samples"] = PUB_SAMPLES
    report["archive_debug"] = ARCHIVE_DEBUG
    REPORT_JSON.write_text(json.dumps(report, indent=1))
    print(f"✓ {len(videos)} videos ({report['new']} new) from {len(out['channels'])} channels; "
          f"dropped {report['dropped']}; unresolved: {report['unresolved'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
