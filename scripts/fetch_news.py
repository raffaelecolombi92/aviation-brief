#!/usr/bin/env python3
"""
Aviation Daily Brief — daily news fetcher.
Runs in GitHub Actions on a schedule. Pulls RSS feeds from aviation trade
press, classifies items into sections, extracts a lead image per article,
and merges everything into data/news.json (rolling archive, deduplicated).
"""
import json, re, sys, html, datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "news.json"
HISTORY_CAP = 120          # max archived items per section
MAX_ITEM_AGE_DAYS = 3      # only ingest items published in the last N days
MAX_OG_FETCHES = 40        # cap article-page fetches per run (for og:image)
TIMEOUT = 8
HEADERS = {"User-Agent": "Mozilla/5.0 (AviationDailyBrief/1.0; personal news aggregator)"}

# Words that mark an item as air-domain; used to filter multi-domain
# defense feeds so land/naval stories don't enter the aviation brief.
AVIATION_WORDS = ["aircraft", "fighter", "jet", "bomber", "helicopter", "rotorcraft",
                  "drone", "uav", "ucav", "air force", "airpower", "air power",
                  "aerial", "tanker", "awacs", "airlift", "f-35", "f-16", "f-15",
                  "b-21", "kc-46", "c-130", "rafale", "eurofighter", "gripen",
                  "loyal wingman", "gcap", "fcas", "hypersonic missile", "air defense", "air defence"]

# ---------------------------------------------------------------
# Feeds. 'section' pins every item from that feed to one section;
# feeds without it are classified per-item by keywords below.
# Edit freely — the script skips any feed that fails.
# ---------------------------------------------------------------
FEEDS = [
    {"url": "https://simpleflying.com/feed/",                 "source": "Simple Flying"},
    {"url": "https://theaircurrent.com/feed/",                "source": "The Air Current"},
    {"url": "https://leehamnews.com/feed/",                   "source": "Leeham News",            "section": "fleet"},
    {"url": "https://airinsight.com/feed/",                   "source": "AirInsight"},
    {"url": "https://worldairlinenews.com/feed/",             "source": "World Airline News",     "section": "network"},
    {"url": "https://www.aerotime.aero/feed",                 "source": "AeroTime"},
    {"url": "https://corporatejetinvestor.com/feed/",         "source": "Corporate Jet Investor", "section": "bizav"},
    {"url": "https://www.moodiedavittreport.com/feed/",       "source": "Moodie Davitt Report",   "section": "airports"},
    {"url": "https://www.greenairnews.com/?feed=rss2",        "source": "GreenAir News",          "section": "policy"},
    {"url": "https://runwaygirlnetwork.com/feed/",            "source": "Runway Girl Network"},
    {"url": "https://www.ainonline.com/rss.xml",              "source": "AIN",                    "section": "bizav"},
    # Defense aviation
    {"url": "https://www.airandspaceforces.com/feed/",        "source": "Air & Space Forces Mag", "section": "defense"},
    {"url": "https://breakingdefense.com/feed/",              "source": "Breaking Defense",       "section": "defense",
     "require": AVIATION_WORDS},
    {"url": "https://www.twz.com/feed",                       "source": "The War Zone",           "section": "defense",
     "require": AVIATION_WORDS},
    {"url": "https://www.defensenews.com/arc/outboundfeeds/rss/category/air/?outputType=xml",
     "source": "Defense News",  "section": "defense"},
]

SECTION_IDS = ["network", "airports", "fleet", "finance", "bizav", "policy", "defense"]

# Keyword classifier, checked in this order (first match wins).
KEYWORDS = [
    ("defense", ["fighter jet", "fighter aircraft", "air force", "military aircraft",
                 "defense contract", "defence contract", "f-35", "f-16", "f-15", "f/a-18",
                 "b-21", "kc-46", "a400m", "c-390", "rafale", "eurofighter", "gripen",
                 "military drone", "ucav", "loyal wingman", "gcap", "fcas", "nato",
                 "lockheed martin", "northrop", "military procurement", "air-to-air",
                 "combat aircraft", "military helicopter", "attack helicopter"]),
    ("bizav",   ["business jet", "bizjet", "private jet", "fractional", "charter operator",
                 "fbo", "netjets", "flexjet", "vistajet", "gulfstream", "bombardier global",
                 "falcon 6x", "falcon 8x", "praetor", "citation", "pilatus pc-24", "hondajet"]),
    ("finance", ["lessor", "leasing", "sale-leaseback", "sale and leaseback", "securitisation",
                 "securitization", " abs ", "financing", "earnings", "quarterly results",
                 "merger", "acquisition", "takeover", "m&a", "ipo", "restructuring",
                 "chapter 11", "insolvency", "administration", "aercap", "avolon",
                 "air lease", "smbc aviation", "bocomm", "dubai aerospace"]),
    ("policy",  ["faa", "easa", "icao", "iata", "regulator", "regulation", "mandate",
                 "saf ", "sustainable aviation fuel", "emissions", "ets", "corsia",
                 "traffic rights", "bilateral", "open skies", "airworthiness directive",
                 "certification ban", "dot ", "caa ", "gcaa", "antitrust", "slots regulation"]),
    ("fleet",   ["order for", "orders ", "firm order", "delivery", "deliveries", "boeing",
                 "airbus", "embraer", "comac", "atr ", "737", "a320", "a321", "a330",
                 "a350", "777x", "777-9", "787", "a220", "e2 ", "engine", "gtf",
                 "pw1100", "leap", "trent", "production rate", "certification",
                 "freighter conversion", "rolls-royce", "cfm", "pratt"]),
    ("airports",["airport", "terminal", "runway", "concession", "duty-free", "duty free",
                 "slot", "hub expansion", "ground handling", "air navigation",
                 "passenger traffic", "aci "]),
    ("network", ["route", "routes", "frequency", "frequencies", "nonstop", "non-stop",
                 "launches flights", "launch flights", "resumes", "new service",
                 "codeshare", "alliance", "joint venture", "interline", "capacity",
                 "network", "destination", "hub strategy"]),
]
AIRLINE_HINTS = ["airline", "airways", "carrier", "airlines", "air "]

def classify(title: str, summary: str) -> str | None:
    text = f" {title.lower()} {summary.lower()} "
    for section, words in KEYWORDS:
        if any(w in text for w in words):
            return section
    if any(w in text for w in AIRLINE_HINTS):
        return "network"
    return None  # not aviation-sector-relevant enough — skip

def clean_summary(raw: str, limit: int = 220) -> str:
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text

def entry_image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            u = m.get("url")
            if u and u.startswith("http"):
                return u
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image") and enc.get("href", "").startswith("http"):
            return enc["href"]
    return None

def og_image(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for sel, attr in ((("meta", {"property": "og:image"}), "content"),
                          (("meta", {"name": "twitter:image"}), "content")):
            tag = soup.find(*sel)
            if tag and tag.get(attr, "").startswith("http"):
                return tag[attr]
    except Exception:
        pass
    return None

def norm_key(item) -> str:
    return re.sub(r"[^a-z0-9]", "", (item.get("url") or item.get("headline") or "").lower())[:140]

def main():
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=MAX_ITEM_AGE_DAYS)

    # Load existing archive
    data = {"generated": None, "sections": {s: [] for s in SECTION_IDS}}
    if DATA_FILE.exists():
        try:
            old = json.loads(DATA_FILE.read_text())
            for s in SECTION_IDS:
                data["sections"][s] = old.get("sections", {}).get(s, [])
        except Exception as e:
            print(f"WARN could not read existing archive: {e}")

    seen = {s: {norm_key(it) for it in data["sections"][s]} for s in SECTION_IDS}
    new_items = {s: [] for s in SECTION_IDS}
    og_budget = MAX_OG_FETCHES

    for feed in FEEDS:
        try:
            parsed = feedparser.parse(feed["url"], request_headers=HEADERS)
            if parsed.bozo and not parsed.entries:
                print(f"SKIP {feed['source']}: feed unreadable")
                continue
        except Exception as e:
            print(f"SKIP {feed['source']}: {e}")
            continue

        count = 0
        for entry in parsed.entries[:25]:
            title = clean_summary(entry.get("title", ""), 200)
            link = entry.get("link", "")
            if not title or not link:
                continue

            # published date
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            day = datetime.date(*pub[:3]) if pub else today
            if day < cutoff:
                continue

            summary = clean_summary(entry.get("summary", "") or entry.get("description", ""))
            req = feed.get("require")
            if req:
                text = f" {title.lower()} {summary.lower()} "
                if not any(w in text for w in req):
                    continue
            section = feed.get("section") or classify(title, summary)
            if section not in SECTION_IDS:
                continue

            item = {
                "headline": title,
                "summary": summary,
                "source": feed["source"],
                "url": link,
                "day": day.isoformat(),
                "img": entry_image(entry),
            }
            if not item["img"] and og_budget > 0:
                item["img"] = og_image(link)
                og_budget -= 1

            key = norm_key(item)
            if key in seen[section]:
                continue
            seen[section].add(key)
            new_items[section].append(item)
            count += 1
        print(f"OK   {feed['source']}: {count} new item(s)")

    # Merge: newest first, cap archive
    total_new = 0
    for s in SECTION_IDS:
        fresh = sorted(new_items[s], key=lambda it: it["day"], reverse=True)
        data["sections"][s] = (fresh + data["sections"][s])[:HISTORY_CAP]
        total_new += len(fresh)

    data["generated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes")
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"DONE {total_new} new item(s) merged → {DATA_FILE}")

if __name__ == "__main__":
    sys.exit(main())
