"""
Discovery layer — solves "I didn't even see it."
Covers scholarships, internships, fellowships, jobs, grants, hackathons.

Polls RSS feeds, filters by your eligibility keywords, auto-categorizes,
and files new matches with status 'seen'.

Run standalone:  python scraper.py
"""

import re
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
import feedparser

import db

HEADERS = {
    # A realistic browser User-Agent, not something that announces
    # itself as a bot — some WordPress sites (often ones running ad
    # networks) serve a "verify you're human" page instead of the real
    # feed to obviously-automated User-Agents, which looks exactly like
    # a successful request that mysteriously contains zero real entries.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
}

# ---------------------------------------------------------------------------
# Sources — add/remove freely
# ---------------------------------------------------------------------------

FEEDS = [
    # Broad opportunity aggregators
    {"name": "Opportunity Desk",           "url": "https://opportunitydesk.org/feed/"},
    # After School Africa removed: their /feed/ URL is a broken/orphaned
    # endpoint (site appears to have been rebuilt as a modern JS app,
    # leaving the old RSS route unmaintained — confirmed via a real
    # browser hanging indefinitely on it too, not just this scraper).
    # The site itself is very much alive; paste individual scholarship
    # links from it directly instead, same as any other manual link.
    {"name": "Opportunities For Africans", "url": "https://www.opportunitiesforafricans.com/feed/"},
    # Scholarship Positions removed: confirmed blocked by Cloudflare's
    # challenge page even via a real browser, not just this scraper —
    # same category as After School Africa's dead /feed/ route.
    {"name": "Scholars World",             "url": "https://scholarsworld.ng/feed/"},
    # Jobs / tech (add Nigerian job boards with RSS here as you find them)
    {"name": "WeWorkRemotely (Programming)", "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss"},
    # RemoteOK removed from RSS: confirmed via a WordPress support thread
    # that their RSS URL is blocked by Cloudflare (403, HTML instead of
    # real feed content) regardless of which variant of the URL is used.
    # Their JSON API is a genuinely different, independently-monitored
    # endpoint (100% uptime tracked daily) — handled as a JSON source
    # below via "type": "remoteok_json" instead of RSS.
    {"name": "RemoteOK", "url": "https://remoteok.com/api", "type": "remoteok_json"},
]

# ---------------------------------------------------------------------------
# "Google as general" — Google itself has no public RSS feed for search
# results, but Google Alerts does, and it's free. One-time setup (~2 min
# per search term you want covered):
#   1. Go to google.com/alerts
#   2. Type a search, e.g.  scholarship Nigeria undergraduate engineering
#   3. Click "Show options":
#        - How often: "At most once a day" (keeps volume sane)
#        - Sources: "Automatic" or narrow to News/Web
#        - Deliver to: "RSS feed"   <- this is the setting that matters;
#          it swaps the button from "Create Alert" (email) to a feed link
#   4. Click the create button, then on your Alerts dashboard click the
#      orange RSS icon next to the new alert and copy that link.
#   5. Paste it below. Repeat for as many search terms as you want
#      (e.g. one for "internship Lagos tech", one for "AI fellowship
#      Africa", one for "graduate trainee Nigeria").
# Any entries below run through the exact same scoring/filtering as
# every other feed — Google Alerts just becomes another source.
# ---------------------------------------------------------------------------

GOOGLE_ALERTS_FEEDS = [
    {"name": "Google Alert: data science internship Nigeria",
     "url": "https://www.google.com/alerts/feeds/00301591919048987695/14998128405369983341"},
    {"name": "Google Alert: AI hackathon Africa",
     "url": "https://www.google.com/alerts/feeds/00301591919048987695/2978852025632999222"},
    {"name": "Google Alert: tech scholarship Nigeria",
     "url": "https://www.google.com/alerts/feeds/00301591919048987695/1506777794465253726"},
]

FEEDS = FEEDS + GOOGLE_ALERTS_FEEDS

# An item must contain at least one INCLUDE keyword...
INCLUDE = [
    "scholarship", "fellowship", "grant", "internship", "intern",
    "hackathon", "competition", "challenge", "funded", "stipend",
    "training program", "cohort", "bootcamp",
    "job", "hiring", "vacancy", "developer", "engineer", "analyst",
    "remote", "graduate program", "graduate trainee",
    # Leadership and volunteer opportunities are their own category —
    # a "Program Lead" or "Volunteer Coordinator" posting won't
    # necessarily say "job"/"internship" anywhere, so without these
    # they'd miss the gate entirely regardless of relevance.
    "leadership", "lead ", "volunteer",
]

# ...and is boosted if it matches your profile:
PROFILE_BOOST = [
    "nigeria", "african", "africa", "engineering", "undergraduate",
    "stem", "tech", "ai", "machine learning", "data science", "python",
    "software", "developer", "electrical", "remote", "junior", "entry level",
    # "graduate" (bare) deliberately removed — you're an undergraduate,
    # not looking for graduate-school content, and the bare word could
    # inappropriately boost postgraduate/master's listings. "graduate
    # program"/"graduate trainee" (entry-level schemes for new grads)
    # stay in INCLUDE as their own specific phrases, unaffected by this.
    # Major Nigerian cities — plenty of listings say "Lagos" or "Abuja"
    # without ever literally saying "Nigeria". Without these, a
    # perfectly legitimate local opportunity (any field, not just
    # tech) could score too low to clear the discovery threshold —
    # confirmed this was actually happening before adding these.
    "lagos", "abuja", "ibadan", "port harcourt", "kano", "enugu",
    "kaduna", "benin city", "owerri", "calabar", "ilorin", "abeokuta",
    # Specific interest areas from an actual project/hackathon
    # portfolio (LLM agents, fintech, fraud detection, predictive
    # maintenance/energy, computer vision) — this is a boost on top of
    # the general tech terms above, not a replacement for them, so a
    # plain "software developer" or "AI" listing still scores fine
    # even without hitting one of these more specific terms.
    "llm", "large language model", "agentic", "multi-agent", "genai",
    "generative ai", "computer vision", "nlp", "fintech",
    "predictive maintenance", "fraud detection", "energy", "azure",
    "hackathon",
    # "data analy" deliberately matches BOTH "data analysis" and "data
    # analyst" via the shared prefix, since job titles ("Data Analyst")
    # and field names ("Data Analysis") use different suffixes and
    # neither was covered by "data science" alone — confirmed both
    # scored zero matches before this was added.
    "data analy",
    # "engineer" (no "-ing") catches job titles like "ML Engineer",
    # "AI Engineer", "Software Engineer", "Data Engineer" that the
    # existing "engineering" term missed entirely, since "engineering"
    # doesn't appear as a substring of "engineer" — it's the field
    # name, not the job title.
    "engineer",
    # Frontend/backend specifically called out, plus leadership and
    # volunteer roles as their own boosted interests, not just gate
    # keywords.
    "frontend", "backend", "full stack", "leadership", "lead ",
    "volunteer",
]

# Hard skips:
EXCLUDE = [
    "phd only", "postdoctoral", "masters only", "faculty", "professor",
    "senior ", "10+ years", "staff engineer", "principal engineer",
    # Platform/community activity notifications — "X submitted Y",
    # "X signed up for Z" — these are peer-activity emails from cohort
    # platforms (e.g. "Jane submitted Week 1"), not opportunity
    # announcements, but can slip past INCLUDE if the body happens to
    # mention "challenge"/"cohort"/etc. Confirmed this was actually
    # happening during real inbox scanning, not a hypothetical.
    "submitted week", "signed up for the",
]


def _parse_remoteok_json(resp, feed: dict) -> tuple:
    """RemoteOK's /api returns a JSON array. The first element is a
    legal/terms notice (not a job) — every source describing this API
    documents that same quirk, so entries without a 'position' field
    are skipped rather than treated as a parse failure."""
    try:
        data = resp.json()
    except Exception as e:
        print(f"  [!] {feed['name']}: couldn't parse JSON response ({e})")
        return [], False

    if not isinstance(data, list):
        print(f"  [!] {feed['name']}: unexpected JSON shape (not a list)")
        return [], False

    items = []
    for entry in data:
        if not isinstance(entry, dict) or "position" not in entry:
            continue  # the legal-notice element, or anything malformed
        title = (entry.get("position") or "").strip()
        company = (entry.get("company") or "").strip()
        full_title = f"{title} at {company}" if company else title
        url = entry.get("url") or entry.get("apply_url") or ""
        if url and not url.startswith("http"):
            url = "https://remoteok.com" + url
        tags = entry.get("tags") or []
        raw_desc = entry.get("description", "")
        desc = re.sub(r"<[^>]+>", " ", raw_desc)[:400]
        summary = f"{desc} tags: {', '.join(tags)}".strip()
        if title and url:
            items.append({"title": full_title, "url": url, "summary": summary,
                          "feed_name": feed["name"]})

    return items, True


def _unwrap_google_redirect(url: str) -> str:
    """Google Alerts wraps every link in its own redirect
    (google.com/url?...&url=<real destination>&...) rather than
    linking to the real page directly. Extract the actual destination
    so we file and extract-checklist-from the real page — otherwise
    'paste this link back for a full checklist' would be reading
    Google's redirect wrapper instead of the actual opportunity."""
    if "google.com/url" not in url:
        return url
    qs = parse_qs(urlparse(url).query)
    real = qs.get("url", [None])[0]
    return real if real else url


def fetch_feed(feed: dict, retries: int = 2) -> tuple:
    """Returns (items, ok). ok=False means this source genuinely
    couldn't be reached or parsed this run — distinct from ok=True
    with an empty items list, which just means a clean, empty feed."""
    resp = None
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(feed["url"], headers=HEADERS, timeout=20)
            resp.raise_for_status()
            break
        except Exception as e:
            last_error = e
            resp = None
            if attempt < retries:
                # DNS blips and momentary connection issues are usually
                # gone a couple seconds later — a short pause and one
                # more try covers most of them without meaningfully
                # slowing down the normal (working) case.
                time.sleep(2)

    if resp is None:
        print(f"  [!] {feed['name']}: {last_error}")
        return [], False

    if feed.get("type") == "remoteok_json":
        return _parse_remoteok_json(resp, feed)

    # feedparser (unlike xml.etree.ElementTree) never throws on a
    # malformed feed — real-world RSS is often slightly invalid XML
    # (stray characters, unescaped symbols), and it does its best to
    # recover entries anyway rather than failing the whole feed.
    parsed = feedparser.parse(resp.content)

    items = []
    for entry in parsed.entries:
        title = re.sub(r"<[^>]+>", "", entry.get("title", "")).strip()
        link = _unwrap_google_redirect(entry.get("link", "").strip())
        raw_desc = entry.get("summary", "") or entry.get("description", "")
        desc = re.sub(r"<[^>]+>", " ", raw_desc)[:500]
        if title and link:
            items.append({"title": title, "url": link, "summary": desc,
                          "feed_name": feed["name"]})

    if not items and getattr(parsed, "bozo", 0):
        print(f"  [!] {feed['name']}: feed had issues, recovered 0 entries "
              f"({getattr(parsed, 'bozo_exception', 'unknown error')})")
        return [], False

    return items, True


def score(item: dict) -> int:
    text = f"{item['title']} {item['summary']}".lower()
    if any(x in text for x in EXCLUDE):
        return 0
    if not any(k in text for k in INCLUDE):
        return 0
    return 1 + sum(1 for k in PROFILE_BOOST if k in text)


def guess_deadline(item: dict) -> str | None:
    text = f"{item['title']} {item['summary']}"
    patterns = [
        r"(?:deadline|closes?|apply by|before)[:\s]*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Z][a-z]+)\s+(\d{4})",
        r"(?:deadline|closes?|apply by|before)[:\s]*([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        r"(\d{4})-(\d{2})-(\d{2})",
    ]
    # Each pattern group order differs (day-month-year / month-day-year /
    # year-month-day) — normalize by trying each candidate string form.
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if not m:
            continue
        groups = m.groups()
        candidates = [" ".join(groups)]  # e.g. "31 August 2026" or "2026 08 31"
        for fmt in ("%d %B %Y", "%B %d %Y", "%Y %m %d"):
            for cand in candidates:
                try:
                    return datetime.strptime(cand, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
    return None


def scan_all(min_score: int = 2) -> tuple:
    """Returns (new_items, failed_feed_names) so callers can tell a
    genuine 'nothing new today' apart from 'couldn't reach anything' —
    the two look identical if you only check whether new_items is empty."""
    # Imported here to avoid a circular import (core imports scraper)
    from core import classify

    new_items = []
    failed = []
    for feed in FEEDS:
        print(f"Scanning {feed['name']}...")
        items, ok = fetch_feed(feed)
        if not ok:
            failed.append(feed["name"])
        for item in items:
            s = score(item)
            if s < min_score:
                continue
            if db.url_exists(item["url"]):
                continue
            deadline = guess_deadline(item)
            category = classify(f"{item['title']} {item['summary']}")
            oid = db.add_opportunity(
                title=item["title"],
                url=item["url"],
                category=category,
                deadline=deadline,
                source=item["feed_name"],
                notes=item["summary"][:200],
            )
            item.update({"id": oid, "deadline": deadline,
                         "score": s, "category": category})
            new_items.append(item)
    new_items.sort(key=lambda x: -x["score"])
    return new_items, failed


if __name__ == "__main__":
    found, failed = scan_all()
    print(f"\n{len(found)} new opportunities filed:")
    for it in found:
        dl = f" | deadline ~{it['deadline']}" if it["deadline"] else ""
        print(f"  [{it['id']}] ({it['category']}, score {it['score']}) {it['title']}{dl}")
    if failed:
        print(f"\n⚠ {len(failed)} source(s) failed this run: {', '.join(failed)}")
