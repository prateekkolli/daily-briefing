#!/usr/bin/env python3
"""
build_news.py - builds news.json for "My Daily Briefing - Simplified".

Usage:
  python build_news.py                      # fetch feeds -> write news.json
  python build_news.py --serve              # fetch, write, then serve http://localhost:8000
  python build_news.py --serve --refresh 60 # also rebuild news.json every 60 minutes
"""
import argparse
import functools
import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "news.json"
USER_AGENT = "DailyBriefing/1.0"


def gnews(query: str) -> str:
    """Google News RSS search URL for a query (US English)."""
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


# ---- Feed config: (url, tag shown on the card) ----
FEEDS = {
    "tech": [
        ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "AI"),
        (gnews("artificial intelligence when:1d"), "AI"),
        (gnews("consumer technology when:1d"), "Tech"),
    ],
    "economy": [
        ("https://feeds.npr.org/1017/rss.xml", "Economy"),
        (gnews("personal finance interest rates inflation when:2d"), "Your Money"),
    ],
    "politics_national": [
        ("https://feeds.npr.org/1014/rss.xml", "National"),
    ],
    "politics_texas": [
        ("https://www.texastribune.org/feeds/latest/", "Texas"),
        (gnews("Texas politics when:2d"), "Texas"),
    ],
    "social": [
        (gnews('"anti-Indian" OR "Hinduphobia" OR "anti-South Asian" OR "anti-Sikh" hate when:7d'), "Tracking"),
        (gnews("racism hate crime United States when:3d"), "Racism"),
    ],
    "science": [
        ("https://www.sciencedaily.com/rss/top/science.xml", "Science"),
        ("https://feeds.npr.org/1007/rss.xml", "Science"),
    ],
}

SPORTS = {
    "mavericks": gnews('"Dallas Mavericks" when:2d'),
    "cowboys": gnews('"Dallas Cowboys" when:2d'),
    "longhorns": gnews('"Texas Longhorns" when:3d'),
}
BREAKING_SPORTS_FEED = "https://www.espn.com/espn/rss/news"

SOCIAL_RESOURCES = [
    {"label": "Report a hate crime: FBI", "url": "https://tips.fbi.gov"},
    {"label": "Workplace discrimination: EEOC", "url": "https://www.eeoc.gov/filing-charge-discrimination"},
    {"label": "Stop AAPI Hate reporting", "url": "https://stopaapihate.org"},
]

LIMITS = {"tech": 3, "economy": 3, "politics_national": 3, "politics_texas": 2, "social": 3, "science": 3}


# ---------------- Fetch & parse ----------------
def fetch(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def clean(text: str, limit: int = 300) -> str:
    """Strip HTML, collapse whitespace, trim to a readable length."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_bytes: bytes):
    """Yield dicts from RSS 2.0 <item> or Atom <entry> elements."""
    root = ET.fromstring(xml_bytes)
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in el:
            name = _local(child.tag)
            if name == "link" and "link" not in fields:
                fields["link"] = child.get("href") or (child.text or "").strip()
            elif name in ("title", "description", "summary", "content") and name not in fields:
                fields[name] = child.text or ""
            elif name == "source" and "source" not in fields:
                fields["source"] = (child.text or "").strip()

        title = clean(fields.get("title", ""), 160)
        summary = clean(fields.get("description") or fields.get("summary") or fields.get("content") or "")
        source = fields.get("source", "")

        # Google News titles look like "Headline - Publisher"
        if " - " in title and not source:
            title, source = title.rsplit(" - ", 1)
        # Google News descriptions usually just repeat the headline
        if summary and title and summary.lower().startswith(title[:40].lower()):
            summary = ""

        if title:
            yield {"title": title, "summary": summary, "source": source, "link": fields.get("link", "")}


def collect(feeds, limit: int):
    items, seen = [], set()
    for url, tag in feeds:
        try:
            for entry in parse_feed(fetch(url)):
                key = re.sub(r"\W+", "", entry["title"].lower())[:60]
                if key in seen:
                    continue
                seen.add(key)
                items.append({"tag": tag, **entry})
                if len(items) >= limit:
                    return items
        except Exception as exc:  # one bad feed shouldn't break the build
            print(f"  ! skipped {url[:70]}… ({exc})")
    return items


# ---------------- Build ----------------
def build() -> dict:
    print("Building news.json …")
    data = {"updated": datetime.now().isoformat(timespec="seconds")}

    for key in ("tech", "economy", "science"):
        items = collect(FEEDS[key], LIMITS[key])
        if items:
            data[key] = items

    politics = {}
    national = collect(FEEDS["politics_national"], LIMITS["politics_national"])
    texas = collect(FEEDS["politics_texas"], LIMITS["politics_texas"])
    if national:
        politics["national"] = national
    if texas:
        politics["texas"] = texas
    if politics:
        data["politics"] = politics

    social_items = collect(FEEDS["social"], LIMITS["social"])
    data["social"] = {"resources": SOCIAL_RESOURCES}
    if social_items:
        data["social"]["items"] = social_items

    sports = {}
    for team, url in SPORTS.items():
        top = collect([(url, "")], 1)
        if top:
            t = top[0]
            sports[team] = {
                "headline": t["title"],
                "summary": t["summary"],
                "source": t["source"],
                "link": t["link"],
            }
    if sports:
        data["sports"] = sports

    breaking = [i["title"] for i in collect([(BREAKING_SPORTS_FEED, "")], 6)]
    if breaking:
        data["breakingSports"] = breaking

    OUT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✓ Wrote {OUT_FILE.name} ({len(json.dumps(data)) // 1024} KB)")
    return data


# ---------------- Serve ----------------
def serve(port: int, refresh_minutes: int | None):
    if refresh_minutes:
        def loop():
            while True:
                time.sleep(refresh_minutes * 60)
                try:
                    build()
                except Exception as exc:
                    print(f"  ! refresh failed: {exc}")
        threading.Thread(target=loop, daemon=True).start()

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(HERE))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"→ Open http://localhost:{port}  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build news.json and optionally serve the briefing site.")
    parser.add_argument("--serve", action="store_true", help="serve the site after building")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--refresh", type=int, metavar="MIN", help="rebuild news.json every MIN minutes while serving")
    args = parser.parse_args()

    build()
    if args.serve:
        serve(args.port, args.refresh)
