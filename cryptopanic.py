import os
import re
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional, Tuple
from contextlib import asynccontextmanager

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

# Optional Redis for dedupe persistence (leave empty to use in-memory only)
REDIS_URL = os.environ.get("REDIS_URL")

# RSS feeds (add/remove freely)
RSS_FEEDS = [
    # General crypto
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss",            # may rate-limit sometimes
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

# ---------------- Persistence (optional Redis) ----------------
class DedupeStore:
    def __init__(self):
        self.memory = set()
        self.redis = None

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

    async def _key(self) -> str:
        return "crypto_news_bot.posted_urls"

    async def has(self, url: str) -> bool:
        if self.redis:
            return await self.redis.sismember(await self._key(), url)
        return url in self.memory

    async def add(self, url: str):
        if self.redis:
            await self.redis.sadd(await self._key(), url)
        else:
            self.memory.add(url)

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
        except Exception as e:
            if attempt == 2:
                raise
            await asyncio.sleep(1.2 * (attempt + 1))
    return ""

async def fetch_rss(session: aiohttp.ClientSession, url: str) -> List[Dict[str, Any]]:
    text = await fetch_text(session, url, timeout=20)
    parsed = feedparser.parse(text)
    items = []
    for e in parsed.entries[:20]:
        link = e.get("link")
        title = e.get("title", "").strip()
        published = e.get("published", "") or e.get("updated", "")
        if link and title:
            items.append({"title": title, "link": link, "published": published, "source": parsed.feed.get("title", "")})
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
    parts = [p.replace("<dot>", ".").strip() for p in parts if p.strip()]
    return parts

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
    seen_idx = set()
    for i, s, _ in ranked:
        if len(chosen) >= max_sentences:
            break
        chosen.append((i, s))
        seen_idx.add(i)

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

async def build_synopsis(session: aiohttp.ClientSession, url: str, title: str) -> str:
    try:
        html = await fetch_text(session, url)
    except Exception as e:
        log.warning(f"Fetch article failed: {e}")
        return ""
    text = None
    if trafilatura:
        text = extract_with_trafilatura(url, html)
    if not text:
        text = extract_with_readability(html)
    if not text:
        text = meta_description(html)
    if not text:
        return ""
    text = re.sub(r"(©|Copyright).*?\d{4}.*", "", text, flags=re.IGNORECASE)
    synopsis = summarize(text, max_sentences=3, max_chars=420)
    return dedupe_headline(title, synopsis)

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
                # Collect fresh items across feeds
                all_items: List[Dict[str, Any]] = []
                for feed_url in RSS_FEEDS:
                    try:
                        items = await fetch_rss(session, feed_url)
                        all_items.extend(items)
                    except Exception as e:
                        log.warning(f"RSS fetch failed for {feed_url}: {e}")

                # sort by "newest-looking" first (published string desc)
                all_items = [
                    x for x in all_items
                    if isinstance(x.get("link"), str) and x.get("title")
                ]
                # naive sort: newest entries often appear first already; keep order

                posted = 0
                for item in all_items:
                    if posted >= POSTS_PER_INTERVAL:
                        break
                    link = item["link"]
                    if await store.has(link):
                        continue
                    synopsis = await build_synopsis(session, link, item["title"])
                    await post_embed(channel, item, synopsis)
                    await store.add(link)
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
