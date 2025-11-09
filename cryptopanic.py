import os
import re
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional, Tuple
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import aiohttp
import discord
import feedparser

# Optional extractors (pure-Python)
try:
    import trafilatura
except Exception:
    trafilatura = None

try:
    from readability import Document
    from bs4 import BeautifulSoup
except Exception:
    Document = None
    BeautifulSoup = None

# ---------------- Config ----------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("TOKEN")
CHANNEL_ID = int(os.environ["CHANNEL_ID"])  # required
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "180"))
POSTS_PER_INTERVAL = int(os.environ.get("POSTS_PER_INTERVAL", "3"))

# recency controls (from previous update)
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "12"))
ALLOW_UNDATED = os.environ.get("ALLOW_UNDATED", "0") == "1"

# optional Redis for cross-restart dedupe
REDIS_URL = os.environ.get("REDIS_URL")

# RSS feeds
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://cryptoslate.com/feed/",
    "https://cointelegraph.com/rss",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto-news-rss-bot")

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# ---------------- Dedupe helpers ----------------
_TRACKING_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content","utm_name","utm_id",
    "gclid","fbclid","mc_cid","mc_eid","ref","ref_src","feature","spm","igshid","ito","s",
}

def normalize_url(url: str) -> str:
    """Normalize URL to improve dedupe across feeds."""
    try:
        p = urlparse(url)
        # lowercase scheme/host
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        path = p.path or ""
        # remove trailing /amp or /amp/
        if path.endswith("/amp/"):
            path = path[:-5]
        elif path.endswith("/amp"):
            path = path[:-4]
        # clean query: drop tracking params, sort others
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in _TRACKING_PARAMS]
        q.sort()
        query = urlencode(q)
        # strip fragments
        frag = ""
        new = urlunparse((scheme, netloc, path, p.params, query, frag))
        return new
    except Exception:
        return url

def title_fingerprint(title: str) -> str:
    """Rough fingerprint for title-based dedupe."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s$%\-]", " ", t)
    words = [w for w in t.split() if len(w) > 2 and w not in {
        "the","and","for","from","with","that","this","are","was","were","has","have","into",
        "amid","over","under","after","before","among","about","its","their","your","our",
        "says","said","new","news","crypto","cryptocurrency"
    }]
    # keep order-agnostic signature
    return " ".join(sorted(set(words)))[:220]

def titles_similar(fp1: str, fp2: str) -> bool:
    s1, s2 = set(fp1.split()), set(fp2.split())
    if not s1 or not s2:
        return False
    jacc = len(s1 & s2) / len(s1 | s2)
    return jacc >= 0.85

# ---------------- Persistence (optional Redis) ----------------
class DedupeStore:
    """
    Stores:
      - normalized URLs
      - canonical URLs
      - title fingerprints (rolling)
    """
    def __init__(self):
        self.url_set = set()
        self.title_fps: List[str] = []
        self.redis = None
        self.TITLE_MAX = 400  # rolling window

    async def setup(self):
        if REDIS_URL:
            try:
                import aioredis
                self.redis = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
                await self.redis.ping()
                log.info("Using Redis for dedupe.")
            except Exception as e:
                log.warning(f"Redis unavailable ({e}); falling back to memory.")
                self.redis = None

    async def _k_urls(self) -> str:
        return "crypto_news_bot.urls"

    async def _k_titles(self) -> str:
        return "crypto_news_bot.title_fps"

    async def has_url(self, url: str) -> bool:
        url = normalize_url(url)
        if self.redis:
            return await self.redis.sismember(await self._k_urls(), url)
        return url in self.url_set

    async def add_url(self, url: str):
        url = normalize_url(url)
        if self.redis:
            await self.redis.sadd(await self._k_urls(), url)
        else:
            self.url_set.add(url)

    async def has_title_like(self, fp: str) -> bool:
        if self.redis:
            # check exact first
            if await self.redis.sismember(await self._k_titles(), fp):
                return True
            # fetch a manageable sample to compare similarity
            existing = await self.redis.smembers(await self._k_titles())
        else:
            existing = list(self.title_fps)

        # near-duplicate check
        for e in existing[-self.TITLE_MAX:]:
            if e == fp or titles_similar(e, fp):
                return True
        return False

    async def add_title(self, fp: str):
        if self.redis:
            await self.redis.sadd(await self._k_titles(), fp)
            # (optional) trim with a separate structure if you want strict bounds
        else:
            self.title_fps.append(fp)
            if len(self.title_fps) > self.TITLE_MAX:
                self.title_fps = self.title_fps[-self.TITLE_MAX:]

store = DedupeStore()

# ---------------- HTTP helpers ----------------
@asynccontextmanager
async def get_session():
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        yield session

async def fetch_text(session: aiohttp.ClientSession, url: str, timeout: int = 25) -> str:
    for attempt in range(3):
        try:
            async with session.get(url, timeout=timeout) as r:
                r.raise_for_status()
                return await r.text()
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(1.2 * (attempt + 1))
    return ""

# ---------------- Date parsing ----------------
def parse_dt(entry: Dict[str, Any]) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        if entry.get(key):
            try:
                return datetime.fromtimestamp(int(feedparser.mktime_tz(entry[key])), tz=timezone.utc)
            except Exception:
                pass
    for key in ("published", "updated"):
        val = entry.get(key)
        if not val:
            continue
        try:
            dt = parsedate_to_datetime(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            continue
    return None

# ---------------- RSS fetch ----------------
async def fetch_rss(session: aiohttp.ClientSession, url: str) -> List[Dict[str, Any]]:
    text = await fetch_text(session, url, timeout=20)
    parsed = feedparser.parse(text)
    items = []
    feed_title = parsed.feed.get("title", "")
    for e in parsed.entries[:40]:
        link = e.get("link")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        dt = parse_dt(e)
        items.append({
            "title": title,
            "link": link,
            "norm_link": normalize_url(link),
            "published": e.get("published") or e.get("updated") or "",
            "source": feed_title,
            "dt": dt,
            "title_fp": title_fingerprint(title),
        })
    return items

# ---------------- Extraction & Summary ----------------
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"Read more.*$", "", t, flags=re.IGNORECASE)
    return t

def split_sentences(text: str) -> List[str]:
    text = clean_text(text)
    text = re.sub(r"(Mr|Ms|Dr|Prof|Jr|Sr|St)\.", r"\1<dot>", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(“\"'])", text)
    return [p.replace("<dot>", ".").strip() for p in parts if p.strip()]

def summarize(text: str, max_sentences: int = 3, max_chars: int = 420) -> str:
    sents = split_sentences(text)
    if not sents:
        return ""
    joined = " ".join(sents)
    if len(joined) <= max_chars and len(sents) <= max_sentences:
        return joined

    words = re.findall(r"[A-Za-z0-9$%\-\.]+", text.lower())
    stop = set(
        "the a an and or of to for from in on at as with by is are was were be has have had "
        "it that this those these you your their our its they he she them we not will would can "
        "could should may might about into over under after before amid among than via says said"
        .split()
    )
    freqs: Dict[str, int] = {}
    for w in words:
        if len(w) < 3 or w in stop:
            continue
        freqs[w] = freqs.get(w, 0) + 1

    def score(s: str, i: int) -> float:
        tokens = re.findall(r"[A-Za-z0-9$%\-\.]+", s.lower())
        if not tokens:
            return 0.0
        dens = sum(freqs.get(t, 0) for t in tokens) / max(len(tokens), 1)
        lead_bonus = 1.0 / (1 + i)
        len_penalty = 1.0 if len(s) < 240 else 0.85
        return dens * 0.7 + lead_bonus * 0.25 + len_penalty * 0.05

    ranked = sorted(((i, s, score(s, i)) for i, s in enumerate(sents)), key=lambda x: x[2], reverse=True)
    chosen: List[Tuple[int, str]] = []
    for i, s, _ in ranked[:max_sentences]:
        chosen.append((i, s))
    chosen.sort(key=lambda x: x[0])
    out = " ".join(s for _, s in chosen)
    if len(out) > max_chars:
        out = out[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return out

def dedupe_headline(title: str, synopsis: str) -> str:
    if not synopsis:
        return ""
    t = re.sub(r"[^A-Za-z0-9 ]+", "", title.lower())
    s = re.sub(r"[^A-Za-z0-9 ]+", "", synopsis.lower())
    if t and t in s:
        synopsis = re.sub(re.escape(title) + r"[:\-–]?\s*", "", synopsis, flags=re.IGNORECASE).strip()
    return synopsis

def extract_canonical(html: str) -> Optional[str]:
    if not BeautifulSoup:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        for sel in ["link[rel=canonical]", "meta[property='og:url']"]:
            tag = soup.select_one(sel)
            if tag:
                u = tag.get("href") or tag.get("content")
                if u and u.startswith("http"):
                    return normalize_url(u)
    except Exception:
        pass
    return None

def extract_with_trafilatura(url: str, html: Optional[str] = None) -> Optional[str]:
    if not trafilatura:
        return None
    try:
        downloaded = html if html else trafilatura.fetch_url(url)
        if not downloaded:
            return None
        txt = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return clean_text(txt or "")
    except Exception:
        return None

def extract_with_readability(html: str) -> Optional[str]:
    if not Document or not BeautifulSoup:
        return None
    try:
        doc = Document(html)
        summary_html = doc.summary()
        soup = BeautifulSoup(summary_html, "lxml")
        text = " ".join(x.get_text(" ", strip=True) for x in soup.find_all(["p", "li"]))
        return clean_text(text)
    except Exception:
        return None

def meta_description(html: str) -> Optional[str]:
    if not BeautifulSoup:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        for sel in [
            "meta[name=description]",
            "meta[property='og:description']",
            "meta[name='twitter:description']",
        ]:
            tag = soup.select_one(sel)
            if tag and tag.get("content"):
                return clean_text(tag["content"])
        p = soup.find("p")
        if p and p.text:
            return clean_text(p.text)
    except Exception:
        pass
    return None

async def build_synopsis_and_canonical(session: aiohttp.ClientSession, url: str, title: str) -> Tuple[str, Optional[str]]:
    """Fetch page once; return (synopsis, canonical_url_if_any)."""
    try:
        html = await fetch_text(session, url)
    except Exception as e:
        log.warning(f"Fetch article failed: {e}")
        return "", None
    canonical = extract_canonical(html)
    text = extract_with_trafilatura(url, html) or extract_with_readability(html) or meta_description(html)
    if not text:
        return "", canonical
    text = re.sub(r"(©|Copyright).*?\d{4}.*", "", text, flags=re.IGNORECASE)
    synopsis = summarize(text, max_sentences=3, max_chars=420)
    return dedupe_headline(title, synopsis), canonical

# ---------------- Posting ----------------
async def post_embed(channel: discord.TextChannel, item: Dict[str, Any], synopsis: str):
    title = item["title"]
    link = item["link"]
    published = item.get("published") or "unknown time"
    source_name = item.get("source") or ""
    description = f"**Synopsis:** {synopsis}" if synopsis else "_Synopsis unavailable_"
    embed = discord.Embed(title=title, url=link, description=description, color=0x2b6cb0)
    small_source = f"{source_name} • `{published}`" if source_name else f"`{published}`"
    embed.add_field(name="Source", value=f"[Open]({link}) • {small_source}", inline=False)
    await channel.send(embed=embed)

# ---------------- Main loop ----------------
@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (id={client.user.id})")
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        raise SystemExit(f"Channel {CHANNEL_ID} not found")

    await store.setup()

    while True:
        try:
            async with get_session() as session:
                # Gather
                items: List[Dict[str, Any]] = []
                for feed_url in RSS_FEEDS:
                    try:
                        items.extend(await fetch_rss(session, feed_url))
                    except Exception as e:
                        log.warning(f"RSS fetch failed for {feed_url}: {e}")

                # Recency filter + newest first
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(hours=MAX_AGE_HOURS)
                fresh = []
                for it in items:
                    dt = it.get("dt")
                    if dt is None and not ALLOW_UNDATED:
                        continue
                    if dt is not None and dt < cutoff:
                        continue
                    fresh.append(it)

                fresh.sort(key=lambda x: x.get("dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

                posted = 0
                for item in fresh:
                    if posted >= POSTS_PER_INTERVAL:
                        break

                    link_norm = item["norm_link"]
                    # quick URL dedupe
                    if await store.has_url(link_norm):
                        continue
                    # quick title dedupe
                    if await store.has_title_like(item["title_fp"]):
                        continue

                    # fetch article once to (a) extract body (b) get canonical URL
                    synopsis, canonical = await build_synopsis_and_canonical(session, item["link"], item["title"])

                    # canonical/url dedupe before posting
                    if canonical and await store.has_url(canonical):
                        continue

                    await post_embed(channel, item, synopsis)

                    # record dedupe keys
                    await store.add_url(link_norm)
                    if canonical:
                        await store.add_url(canonical)
                    await store.add_title(item["title_fp"])

                    posted += 1

                if posted == 0:
                    log.info("No new posts to send this cycle.")
        except Exception as e:
            log.exception(f"Loop error: {e}")

        await asyncio.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN")
    client.run(DISCORD_TOKEN)
