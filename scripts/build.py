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

MAX_PER_CHANNEL = 300   # library cap per channel (oldest dropped first)
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
              "UDP", "DNS", "SSD", "JWST", "LHC", "QED", "QCD", "EV", "SpaceX"}


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

    t = re.sub(r"\b[A-Z][A-Z']{3,}\b", fix, t)
    return re.sub(r"\s{2,}", " ", t).strip(" -|·")


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------

def fetch_feed(channel_id: str) -> tuple[str, list[dict]]:
    xml = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
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

def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="no network; reuse existing data")
    ap.add_argument("--workers", type=int, default=12)
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
        key = (c.get("handle") or c.get("channel_id") or "").lstrip("@")
        if not key:
            continue
        if c.get("topic") not in topics:
            print(f"! {key}: unknown topic {c.get('topic')!r}", file=sys.stderr)
            continue
        cid = c.get("channel_id") or ids_cache.get(key.lower())
        chans.append({**c, "key": key, "cid": cid})

    if not args.offline:
        todo = [c for c in chans if not c["cid"]]
        with cf.ThreadPoolExecutor(args.workers) as ex:
            for c, cid in zip(todo, ex.map(lambda c: resolve_handle(c["key"]), todo)):
                if cid:
                    c["cid"] = cid
                    ids_cache[c["key"].lower()] = cid
                    report["resolved"].append(c["key"])
                    print(f"  resolved @{c['key']} -> {cid}")
                else:
                    report["unresolved"].append(c["key"])
                    print(f"! could not resolve @{c['key']}", file=sys.stderr)

    # ---- fetch feeds ---------------------------------------------------------
    channel_meta: dict[str, dict] = {}
    for c in chans:
        channel_meta[c["key"]] = {"key": c["key"], "name": prev_ch.get(c["key"], {}).get("name", c["key"]),
                                  "topic": c["topic"], "cid": c.get("cid")}

    feeds: dict[str, tuple[str, list[dict]]] = {}
    if not args.offline:
        live = [c for c in chans if c.get("cid")]

        def grab(c):
            try:
                return c["key"], fetch_feed(c["cid"])
            except Exception as e:  # noqa: BLE001
                return c["key"], e

        with cf.ThreadPoolExecutor(args.workers) as ex:
            for key, res in ex.map(grab, live):
                if isinstance(res, Exception):
                    report["feed_errors"].append(f"{key}: {res}")
                    print(f"! feed {key}: {res}", file=sys.stderr)
                else:
                    feeds[key] = res

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
    new = [(c, e) for c, e in candidates if e["id"] not in library]
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
            "p": e["published"][:10],
            "d": snippet(e["desc"]),
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
    # topic sanity
    for v in videos:
        if v["tp"] not in topics:
            v["tp"] = next(iter(topics))
    videos.sort(key=lambda v: (v.get("p", ""), v["id"]), reverse=True)

    used = {v.get("ch") for v in videos}
    out = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "topics": [{"key": k, "name": n} for k, n in topics.items()],
        "channels": [{"key": m["key"], "name": m["name"], "topic": m["topic"]}
                     for m in channel_meta.values() if m["key"] in used],
        "videos": videos,
    }
    VIDEOS_JSON.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    IDS_JSON.write_text(json.dumps(dict(sorted(ids_cache.items())), indent=1))
    report["total"] = len(videos)
    REPORT_JSON.write_text(json.dumps(report, indent=1))
    print(f"✓ {len(videos)} videos ({report['new']} new) from {len(out['channels'])} channels; "
          f"dropped {report['dropped']}; unresolved: {report['unresolved'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
