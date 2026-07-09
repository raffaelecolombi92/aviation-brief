#!/usr/bin/env python3
"""
Aviation Daily Brief — daily news fetcher.
Runs in GitHub Actions on a schedule. Pulls RSS feeds from aviation trade
press, classifies items into sections, extracts a lead image per article,
and merges everything into data/news.json (rolling archive, deduplicated).
"""
import json, os, re, sys, html, datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "news.json"
HISTORY_CAP = 120          # max archived items per section
MAX_ITEM_AGE_DAYS = 3      # only ingest items published in the last N days
BACKFILL_DAYS = 14         # empty sections may backfill this deep
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
    {"url": ["https://www.ainonline.com/rss.xml",
             "https://www.ainonline.com/aviation-news/rss.xml",
             "https://www.ainonline.com/rss"],
     "source": "AIN", "section": "bizav"},
    {"url": "https://privatejetcardcomparisons.com/feed/",     "source": "Private Jet Card Comparisons", "section": "bizav"},
    {"url": "https://www.businessairportinternational.com/feed", "source": "Business Airport International", "section": "bizav"},
    # Defense aviation
    {"url": "https://www.airandspaceforces.com/feed/",        "source": "Air & Space Forces Mag", "section": "defense"},
    {"url": "https://breakingdefense.com/feed/",              "source": "Breaking Defense",       "section": "defense",
     "require": AVIATION_WORDS},
    {"url": "https://www.twz.com/feed",                       "source": "The War Zone",           "section": "defense",
     "require": AVIATION_WORDS},
    {"url": "https://www.defensenews.com/arc/outboundfeeds/rss/category/air/?outputType=xml",
     "source": "Defense News",  "section": "defense"},
]

SECTION_IDS = ["network", "airports", "fleet", "finance", "bizav", "policy", "defense", "others"]

# Publisher-supplied RSS categories/tags, mapped to our sections.
# Checked BEFORE keyword scoring — the source's own categorisation is
# usually the most reliable signal. Order matters: most specific first.
CATEGORY_MAP = [
    ("bizav",    ["business aviation", "bizav", "business jet", "private jet",
                  "private aviation", "general aviation", "fbo", "charter", "fractional"]),
    ("defense",  ["defense", "defence", "military", "air force", "combat"]),
    ("finance",  ["finance", "leasing", "lessor", "aircraft finance", "m&a",
                  "earnings", "investment", "deals"]),
    ("policy",   ["regulation", "regulatory", "policy", "sustainability",
                  "environment", "saf", "emissions", "safety"]),
    ("airports", ["airport", "airports", "infrastructure", "ground handling",
                  "duty free", "duty-free"]),
    ("fleet",    ["manufacturer", "manufacturers", "oem", "aerospace", "aircraft",
                  "engines", "mro", "maintenance", "deliveries", "orders"]),
    ("network",  ["airline", "airlines", "routes", "route development",
                  "network", "alliances", "carriers"]),
]

def section_from_tags(entry) -> str | None:
    """Use the publisher's own category tags when present."""
    tags = [t.get("term", "").lower() for t in (entry.get("tags") or []) if t.get("term")]
    if not tags:
        return None
    joined = " | ".join(tags)
    for section, words in CATEGORY_MAP:
        if any(w in joined for w in words):
            return section
    return None

# Keyword classifier, checked in this order (first match wins).
KEYWORDS = [
    ("defense", ["fighter jet", "fighter aircraft", "air force", "military aircraft",
                 "defense contract", "defence contract", "f-35", "f-16", "f-15", "f/a-18",
                 "b-21", "kc-46", "a400m", "c-390", "rafale", "eurofighter", "gripen",
                 "military drone", "ucav", "loyal wingman", "gcap", "fcas", "nato",
                 "lockheed martin", "northrop", "military procurement", "air-to-air",
                 "combat aircraft", "military helicopter", "attack helicopter"]),
    ("bizav",   ["business jet", "bizjet", "private jet", "business aviation",
                 "private aviation", "fractional", "jet card", "charter operator",
                 "air charter", "fbo", "netjets", "flexjet", "vistajet", "jet aviation",
                 "gulfstream", "bombardier global", "challenger 3500", "global 7500",
                 "global 8000", "falcon 6x", "falcon 8x", "falcon 10x", "praetor",
                 "phenom", "citation", "pilatus pc-24", "pc-12", "hondajet",
                 "king air", "nbaa", "ebace", "mebaa"]),
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
    """Scored classification: every section is scored on keyword hits
    (title hits count double); highest score wins. Ties resolve by
    KEYWORDS order (most specific sections first)."""
    t = f" {title.lower()} "
    s = f" {summary.lower()} "
    best, best_score = None, 0
    for section, words in KEYWORDS:
        score = sum(2 for w in words if w in t) + sum(1 for w in words if w in s)
        if score > best_score:
            best, best_score = section, score
    if best:
        return best
    if any(w in t + s for w in AIRLINE_HINTS):
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

def llm_reclassify(items, chunk_size=40):
    """Second-pass classification by Claude. Sends headline+summary batches
    and reassigns each item's section; 'drop' removes non-industry items.
    Any failure leaves the keyword-based assignment untouched."""
    api_key = os.environ["ANTHROPIC_API_KEY"]
    valid = set(SECTION_IDS)
    reassigned = 0
    for start in range(0, len(items), chunk_size):
        chunk = items[start:start + chunk_size]
        numbered = "\n".join(
            f"{i+1}. {it['headline']} — {it['summary'][:150]}" for i, it in enumerate(chunk))
        prompt = (
            "You are the section editor of an aviation industry news briefing. "
            "Assign each numbered story to exactly one section:\n"
            "- network: airline routes, capacity, alliances, airline strategy\n"
            "- airports: airport infrastructure, concessions, traffic results, slots (the airport itself is the subject, "
            "not merely the location of an airline story)\n"
            "- fleet: commercial aircraft orders/deliveries, OEM programmes, engines, supply chain\n"
            "- finance: leasing, aircraft trading, financings, airline earnings, M&A\n"
            "- bizav: business/private aviation\n"
            "- policy: regulators, traffic rights, SAF mandates, safety directives\n"
            "- defense: military aviation\n"
            "- others: aviation-adjacent or general-interest content that is not core industry news "
            "(travel features, passenger-experience pieces, listicles, trip reports, historical retrospectives)\n\n"
            f"Stories:\n{numbered}\n\n"
            "Respond with ONLY a raw JSON array of section ids in order, one per story, "
            'e.g. ["network","others","fleet"]. No other text.')
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5",
                      "max_tokens": 1500,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60)
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
            text = text.replace("```json", "").replace("```", "").strip()
            labels = json.loads(text[text.index("["):text.rindex("]") + 1])
            if len(labels) != len(chunk):
                print(f"LLM  batch {start//chunk_size+1}: length mismatch, keeping keyword sections")
                continue
            for it, label in zip(chunk, labels):
                it["v"] = 1  # AI-verified; won't be re-reviewed on future runs
                if label not in valid:
                    continue
                if label != it["_sec"]:
                    it["_sec"] = label
                    reassigned += 1
        except Exception as e:
            print(f"LLM  batch {start//chunk_size+1} failed ({e}); keeping keyword sections")
    print(f"LLM  reclassification: {reassigned} item(s) moved")


def main():
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=MAX_ITEM_AGE_DAYS)
    deep_cutoff = today - datetime.timedelta(days=BACKFILL_DAYS)  # for empty sections

    # Load existing archive
    data = {"generated": None, "sections": {s: [] for s in SECTION_IDS}}
    if DATA_FILE.exists():
        try:
            old = json.loads(DATA_FILE.read_text())
            for s in SECTION_IDS:
                data["sections"][s] = old.get("sections", {}).get(s, [])
        except Exception as e:
            print(f"WARN could not read existing archive: {e}")

    seen = {norm_key(it) for s in SECTION_IDS for it in data["sections"][s]}
    pending = []          # new items awaiting final section assignment
    og_budget = MAX_OG_FETCHES

    for feed in FEEDS:
        urls = feed["url"] if isinstance(feed["url"], list) else [feed["url"]]
        parsed = None
        for u in urls:
            try:
                # fetch with requests first so we see real HTTP status in the log
                r = requests.get(u, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code != 200:
                    print(f"TRY  {feed['source']}: {u} -> HTTP {r.status_code}")
                    continue
                cand = feedparser.parse(r.content)
                if cand.entries:
                    parsed = cand
                    break
                print(f"TRY  {feed['source']}: {u} -> 0 entries")
            except Exception as e:
                print(f"TRY  {feed['source']}: {u} -> {e}")
        if parsed is None:
            print(f"SKIP {feed['source']}: no working feed URL")
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
            if day < deep_cutoff:
                continue

            summary = clean_summary(entry.get("summary", "") or entry.get("description", ""))
            req = feed.get("require")
            if req:
                text = f" {title.lower()} {summary.lower()} "
                if not any(w in text for w in req):
                    continue
            section = feed.get("section") or section_from_tags(entry) or classify(title, summary)
            if section not in SECTION_IDS:
                continue

            # Recency: normally last MAX_ITEM_AGE_DAYS, but an empty section
            # is allowed to backfill deeper so it doesn't sit blank for days.
            if day < cutoff and not (day >= deep_cutoff and not data["sections"][section]):
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
            if key in seen:
                continue
            seen.add(key)
            item["_sec"] = section
            pending.append(item)
            count += 1
        print(f"OK   {feed['source']}: {count} new item(s)")

    # Second pass: AI classification via the Anthropic API.
    # Activates automatically when the ANTHROPIC_API_KEY secret is set.
    # Reviews new items PLUS any archive items not yet AI-verified, so the
    # entire archive is re-sorted the first time the key is active, and only
    # new items are reviewed on subsequent runs.
    review = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        for s in SECTION_IDS:
            keep = []
            for it in data["sections"][s]:
                if it.get("v"):
                    keep.append(it)
                else:
                    it["_sec"] = s
                    it["_old"] = True
                    review.append(it)
            data["sections"][s] = keep
        if review:
            print(f"REVIEW {len(review)} unverified archive item(s) queued for AI sorting")
        if pending or review:
            llm_reclassify(pending + review)

    new_items = {s: [] for s in SECTION_IDS}
    moved_back = 0
    for it in pending + review:
        sec = it.pop("_sec", None)
        was_old = it.pop("_old", False)
        if sec in SECTION_IDS:
            new_items[sec].append(it)
            if was_old:
                moved_back += 1

    # Merge: combine reviewed/new items with kept archive, newest day first
    total_new = len(pending)
    for s in SECTION_IDS:
        combined = new_items[s] + data["sections"][s]
        combined.sort(key=lambda it: it.get("day", ""), reverse=True)
        data["sections"][s] = combined[:HISTORY_CAP]

    data["generated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes")
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"DONE {total_new} new item(s) merged → {DATA_FILE}")

if __name__ == "__main__":
    sys.exit(main())
