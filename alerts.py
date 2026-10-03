import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
 
# Most wanted first. Edit these any time.
ARTISTS = ["Morgan Wallen", "Zach Bryan", "Tucker Wetmore", "Luke Combs", "Ella Langley"]
SITES = {
    "Morgan Wallen": "https://morganwallen.com",
    "Zach Bryan": "https://zachbryan.com",
    "Tucker Wetmore": "https://tuckerwetmore.com",
    "Luke Combs": "https://lukecombs.com",
    "Ella Langley": "https://ellalangley.com",
}
PAGES = ["", "/tour", "/music", "/news"]  # pages checked on each official site
 
SEARCH_TOPICS = "tour OR album OR presale OR tickets OR dates"
KEYWORDS = re.compile(
    r"\b(tour|dates|album|presale|pre-sale|tickets?|on[- ]sale|announces?|announced|deluxe|residency|festival|new music|vip)\b",
    re.I,
)
TICKET_HOSTS = re.compile(r"(ticketmaster|livenation|axs\.com|seetickets|eventbrite|ticketweb|gigantic)", re.I)
EXCLUDE = re.compile(
    r"(/products?/|/collections?/|/cart|/account|/policies|privacy|terms|cookie|"
    r"instagram\.com|facebook\.com|twitter\.com|//x\.com|tiktok\.com|youtube\.com|"
    r"\.(jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?|pdf)(\?|$))",
    re.I,
)
UA = "Mozilla/5.0 (compatible; tour-alerts/1.0)"
SEEN_FILE = "seen.json"
TM_KEY = os.environ.get("TICKETMASTER_KEY", "")  # optional, free from developer.ticketmaster.com
LONDON = ZoneInfo("Europe/London")
MAX_POSTS_PER_RUN = 15
MAX_SEEN = 8000
 
 
class State:
    def __init__(self, items):
        self.list = list(items)
        self.set = set(items)
 
    def has(self, key):
        return key in self.set
 
    def add(self, key):
        if key in self.set:
            return False
        self.set.add(key)
        self.list.append(key)
        return True
 
    def save(self):
        markers = [k for k in self.list if k.startswith("seeded:")]
        rest = [k for k in self.list if not k.startswith("seeded:")][-MAX_SEEN:]
        with open(SEEN_FILE, "w") as f:
            json.dump(markers + rest, f)
 
 
def get(url):
    time.sleep(1)  # be polite to the sites we visit
    for attempt in range(2):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                time.sleep(8)  # rate limited: wait and try once more
                continue
            raise
 
 
def send(text):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    urllib.request.urlopen(req, timeout=30).read()
 
 
# ---------- News (Google News RSS) ----------
 
def fetch_news(artist):
    query = f'"{artist}" ({SEARCH_TOPICS}) when:3d'
    params = urllib.parse.urlencode({"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})
    root = ET.fromstring(get("https://news.google.com/rss/search?" + params).encode("utf-8"))
    for item in root.iter("item"):
        link = item.findtext("link") or ""
        yield {"id": item.findtext("guid") or link, "title": item.findtext("title") or "", "link": link}
 
 
def title_key(title):
    # Drop the " - Outlet" suffix so the same story from different outlets matches.
    base = title.rsplit(" - ", 1)[0].lower()
    return "t:" + re.sub(r"[^a-z0-9]+", " ", base).strip()[:60]
 
 
def check_news(state, artist, first_run, out):
    try:
        items = list(fetch_news(artist))
    except Exception as exc:
        print(f"News fetch failed for {artist}: {exc}")
        return
    for item in items:
        if not state.add(item["id"]) or first_run:
            continue
        if artist.lower() not in item["title"].lower() or not KEYWORDS.search(item["title"]):
            continue
        if state.add(title_key(item["title"])):
            out.append(("🎫", artist, item["title"], item["link"]))
 
 
# ---------- Official websites ----------
 
class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.lines = [], []
        self._skip, self._href, self._text = 0, None, []
 
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []
 
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
 
    def handle_data(self, data):
        if self._skip:
            return
        text = " ".join(data.split())
        if self._href is not None and text:
            self._text.append(text)
        if len(text) >= 12:
            self.lines.append(text)
 
 
def watch_page(state, artist, url, out):
    try:
        html = get(url)
    except Exception as exc:
        if "404" not in str(exc):  # missing pages are normal, so stay quiet
            print(f"Skipped {url}: {exc}")
        return
    marker = "seeded:" + url
    first = not state.has(marker)
    state.add(marker)
    page = Page()
    page.feed(html)
    for href, text in page.links:
        if re.match(r"(mailto:|tel:|javascript:|#)", href, re.I):
            continue
        full = urllib.parse.urljoin(url, href).split("#")[0]
        if not full.startswith("http") or EXCLUDE.search(full):
            continue
        if not state.add("k:" + full) or first:
            continue
        if KEYWORDS.search(text) or KEYWORDS.search(full) or TICKET_HOSTS.search(full):
            out.append(("🌐", artist, f"New link on official site: {text or full}", full))
    for line in page.lines:
        key = "l:" + hashlib.md5(line.encode()).hexdigest()[:12]
        if state.add(key) and not first and KEYWORDS.search(line):
            out.append(("🌐", artist, f"Official site update: {line[:200]}", url))
 
 
def sitemap_urls(base):
    locs = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
    try:
        found = locs.findall(get(base + "/sitemap.xml"))
    except Exception as exc:
        print(f"No sitemap for {base}: {exc}")
        return set()
    urls, nested = set(), 0
    for loc in found:
        if not loc.endswith(".xml"):
            urls.add(loc)
        elif nested < 6 and not re.search(r"product|collection|image", loc, re.I):
            nested += 1
            try:
                urls.update(locs.findall(get(loc)))
            except Exception:
                pass
    return urls
 
 
def watch_sitemap(state, artist, base, out):
    urls = sitemap_urls(base)
    if not urls:
        return
    marker = "seeded:" + base + "/sitemap.xml"
    first = not state.has(marker)
    state.add(marker)
    for url in sorted(urls):
        if not EXCLUDE.search(url) and state.add("k:" + url) and not first:
            out.append(("🌐", artist, "New page on official site", url))
 
 
# ---------- Ticketmaster (dates and presales) and releases (Apple Music) ----------
 
def when(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LONDON).strftime("%a %d %b %H:%M")
    except Exception:
        return iso or "time TBC"
 
 
def fetch_tm(artist):
    params = urllib.parse.urlencode(
        {"apikey": TM_KEY, "keyword": artist, "size": 200, "sort": "date,asc", "locale": "*"}
    )
    data = json.loads(get("https://app.ticketmaster.com/discovery/v2/events.json?" + params))
    for ev in data.get("_embedded", {}).get("events", []):
        names = [a.get("name", "").lower() for a in ev.get("_embedded", {}).get("attractions", [])]
        if artist.lower() in names:
            yield ev
 
 
def check_tickets(state, artist, out):
    if not TM_KEY:
        return
    try:
        events = list(fetch_tm(artist))
    except Exception as exc:
        print(f"Ticketmaster failed for {artist}: {exc}")
        return
    print(f"Ticketmaster: {len(events)} events found for {artist}")
    marker = "seeded:tm:" + artist
    first = not state.has(marker)
    state.add(marker)
    dates, presales, link = [], [], ""
    for ev in events:
        venue = (ev.get("_embedded", {}).get("venues") or [{}])[0]
        place = ", ".join(
            x for x in [venue.get("city", {}).get("name"), venue.get("country", {}).get("countryCode")] if x
        )
        if state.add("e:" + ev["id"]):
            day = ev.get("dates", {}).get("start", {}).get("localDate", "date TBC")
            dates.append(f"• {day} {place} ({venue.get('name', '')})")
            link = link or ev.get("url", "")
        for ps in ev.get("sales", {}).get("presales") or []:
            key = f"p:{artist}|{ps.get('name')}|{ps.get('startDateTime')}"
            if state.add(key):
                presales.append(f"• {ps.get('name', 'Presale')}: opens {when(ps.get('startDateTime'))}")
                link = link or ev.get("url", "")
    if first or not (dates or presales):
        return
    lines = []
    if dates:
        lines.append(f"{len(dates)} new date(s):")
        lines += dates[:8]
        if len(dates) > 8:
            lines.append(f"...and {len(dates) - 8} more")
    if presales:
        lines.append("Presales (UK time):")
        lines += presales[:5]
    out.append(("🎟️", artist, "\n".join(lines), link))
 
 
def check_releases(state, artist, out):
    params = urllib.parse.urlencode(
        {"term": artist, "entity": "album", "attribute": "artistTerm", "limit": 50, "country": "gb"}
    )
    try:
        results = json.loads(get("https://itunes.apple.com/search?" + params)).get("results", [])
    except Exception as exc:
        print(f"Release check failed for {artist}: {exc}")
        return
    marker = "seeded:itunes:" + artist
    first = not state.has(marker)
    state.add(marker)
    for r in results:
        if r.get("artistName", "").lower() != artist.lower():
            continue
        if state.add("a:" + str(r.get("collectionId"))) and not first:
            title = f"New release: {r.get('collectionName')} ({r.get('releaseDate', '')[:10]})"
            out.append(("💿", artist, title, r.get("collectionViewUrl", "")))
 
 
# ---------- Run ----------
 
def main():
    first_run = not os.path.exists(SEEN_FILE)
    state = State([] if first_run else json.load(open(SEEN_FILE)))
    out = []
    print("Ticketmaster key found" if TM_KEY else "Ticketmaster key NOT set")
 
    for artist in ARTISTS:
        check_news(state, artist, first_run, out)
        check_tickets(state, artist, out)
        check_releases(state, artist, out)
        base = SITES.get(artist)
        if base:
            watch_sitemap(state, artist, base, out)
            for path in PAGES:
                watch_page(state, artist, base + path, out)
 
    if first_run:
        send("Alerts are live. Watching news and official sites for: " + ", ".join(ARTISTS))
    for icon, artist, headline, link in out[:MAX_POSTS_PER_RUN]:
        send(f"{icon} {artist}\n{headline}\n{link}")
    if len(out) > MAX_POSTS_PER_RUN:
        send(f"...and {len(out) - MAX_POSTS_PER_RUN} more new items this run.")
 
    state.save()
    # Weekly file change keeps the schedule alive on GitHub.
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    try:
        old = open("heartbeat.txt").read()
    except OSError:
        old = ""
    if old != week:
        open("heartbeat.txt", "w").write(week)
    print(f"Posted {min(len(out), MAX_POSTS_PER_RUN)} alerts.")
 
 
if __name__ == "__main__":
    main()
