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

MAX_PER_CHANNEL = 1000  # library cap per channel (oldest dropped first)
DESC_CHARS = 280

# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401
        return None


_no_redirect_opener = urllib.request.build_opener(NoRedirect)


def http_get(url: str, timeout: float = 20, retries: int = 3) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": COOKIE,
                                               "Accept-Language": "en-US,en;q=0.8"})
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
    r"\bdon'?t (buy|watch)\b|\bend of\b.*\?|\bis (dead|over)\b",
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


def caps_ratio(title: str) -> float:
    words = [w for w in re.findall(r"[A-Za-z]{3,}", title)]
    if not words:
        return 0.0
    return sum(1 for w in words if w.isupper() and w not in KEEP_UPPER) / len(words)


def is_clickbait(title: str) -> bool:
    return bool(CLICKBAIT.search(title)) or caps_ratio(title) >= 0.5 or "!!" in title or "?!" in title


def calm_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", html.unescape(title))
    t = EMOJI.sub("", t)
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
    t = re.sub(r"\b[A-Z][A-Z']{3,}\b", fix, t)
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
            "desc": desc,
        })
    return name, out


# Handles both "3 weeks ago" and YouTube's compact "3w ago" / "9mo ago" / "1y ago".
_REL = re.compile(r"(\d+)\s*(seconds?|secs?|s|minutes?|mins?|mo|months?|m|hours?|hrs?|h|"
                  r"days?|d|weeks?|wks?|w|years?|yrs?|y)\b\.?\s*ago", re.I)


_REL_RU = re.compile(r"(\d+)\s*(секунд\w*|сек\.?|минут\w*|мин\.?|час\w*|ч\.|дн\w*|день|"
                     r"недел\w*|нед\.?|месяц\w*|мес\.?|год\w*|лет)\s*назад", re.I)
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
            "desc": _txt(r.get("descriptionSnippet")),
            "approx_date": True,
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
        views = next((parse_views(p) for p in parts if "view" in p.lower() or "просмотр" in p.lower()), None)
        out.append({"id": vid, "title": title, "published": rel_to_iso(pub),
                    "href": f"https://www.youtube.com/watch?v={vid}", "views": 1 if pub else 0,
                    "view_count": views, "desc": "", "approx_date": True})
    return out


def _continuation(node) -> str | None:
    """The paging token of a video list: the continuationItemRenderer that is
    the *last entry* of the grid (richGridRenderer.contents / gridRenderer.items)
    or of a continuation response's continuationItems. Tokens nested deeper
    (shelves, Home tab, menus) are never followed."""
    lists = ([g.get("contents") for g in _walk(node, "richGridRenderer")] +
             [g.get("items") for g in _walk(node, "gridRenderer")] +
             list(_walk(node, "continuationItems")))
    for lst in lists:
        if isinstance(lst, list) and lst and "continuationItemRenderer" in lst[-1]:
            for c in _walk(lst[-1], "continuationCommand"):
                if isinstance(c, dict) and c.get("token"):
                    return c["token"]
    return None


def http_post_json(url: str, payload: dict, retries: int = 3) -> dict:
    body = json.dumps(payload).encode()
    last: Exception | None = None
    for i in range(retries):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": UA, "Cookie": COOKIE, "Content-Type": "application/json",
            "Origin": "https://www.youtube.com", "Accept-Language": "en-US,en;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


ARCHIVE_DEBUG: list = []
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
    page = http_get(f"https://www.youtube.com/channel/{channel_id}/videos?hl={hl}&gl=US")
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
                grid = http_post_json(endpoint, {"context": ctx, "browseId": channel_id, "params": params})
                dbg["tab"] = "videos-tab-browsed"
            except Exception as e:  # noqa: BLE001
                dbg["tab_error"] = str(e)[:200]
    out = _parse_video_items(grid) if grid is not None else []
    pageable = bool(out)
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
            resp = http_post_json(endpoint, {"context": ctx, "continuation": token})
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


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


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
                                  "topic": c["topic"], "cid": c.get("cid")}

    feeds: dict[str, tuple[str, list[dict]]] = {}
    # backfill.json: {channel key: {"cid": ..., "cap": cap used, "n": videos fetched}}
    # (legacy entries were a bare channel-id string, fetched under a cap of 300).
    have = collections.Counter(v.get("ch") for v in library.values())
    backfill: dict[str, dict] = {k: (v if isinstance(v, dict) else {"cid": v, "cap": 300, "n": 300 if have.get(k, 0) >= 270 else have.get(k, 0)})
                                 for k, v in load_json(BACKFILL_JSON, {}).items()}

    def needs_archive(c) -> bool:
        b = backfill.get(c["key"])
        if not b or b.get("cid") != c["cid"] or b.get("hl", "en") != c["hl"]:
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
                    backfill[key] = {"cid": c_["cid"], "cap": MAX_PER_CHANNEL, "n": len(res[1]), "hl": c_["hl"]}
                    report["archived"].append(f"{key}: {len(res[1])}")
                    print(f"  archived {key}: {len(res[1])} videos")
                if isinstance(res, Exception):
                    report["feed_errors"].append(f"{key}: {res}")
                    print(f"! feed {key}: {res}", file=sys.stderr)
                else:
                    feeds[key] = res

    # ---- channel avatars (so the Channels page shows who each one really is) --
    if not args.offline and AVATARS:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        for cid, url in AVATARS.items():
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
            **({"vc": e["view_count"]} if e.get("view_count") else {}),
            **({"cb": 1} if is_clickbait(e["title"]) else {}),
        }
        if not is_clickbait(e["title"]):
            library[e["id"]].pop("cb", None)
        report["new"] += int(is_new)

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
        if v.get("ev") or v.get("ch") in keep_keys:
            by_ch.setdefault(v.get("ch") or "_ev", []).append(v)
    videos = []
    for k, vs in by_ch.items():
        vs.sort(key=lambda v: v.get("p", ""), reverse=True)
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
        "topics": [{"key": k, "name": n} for k, n in topics.items()],
        "channels": [{"key": m["key"], "name": m["name"], "topic": m["topic"], "cid": m.get("cid"),
                      **({"av": 1} if m.get("cid") and (AVATAR_DIR / f"{m['cid']}.jpg").exists() else {})}
                     for m in channel_meta.values() if m["key"] in used],
        "videos": videos,
    }
    VIDEOS_JSON.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    IDS_JSON.write_text(json.dumps(dict(sorted(ids_cache.items())), indent=1))
    if not args.offline:
        BACKFILL_JSON.write_text(json.dumps(dict(sorted(backfill.items())), indent=1, ensure_ascii=False))
    report["total"] = len(videos)
    report["pub_samples"] = PUB_SAMPLES
    report["archive_debug"] = ARCHIVE_DEBUG
    REPORT_JSON.write_text(json.dumps(report, indent=1))
    print(f"✓ {len(videos)} videos ({report['new']} new) from {len(out['channels'])} channels; "
          f"dropped {report['dropped']}; unresolved: {report['unresolved'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
