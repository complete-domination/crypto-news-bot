import os
import re
import html as ihtml
import asyncio
import logging
from typing import List, Dict, Optional, Tuple, Set

import aiohttp
import feedparser
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import dateutil.parser

# ---------------- Env / Config ----------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")            # required
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))        # required

# Polling + output
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "2"))
SYNOPSIS_MAX_CHARS = int(os.getenv("SYNOPSIS_MAX_CHARS", "900"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))

# Time filtering
HOUR_WINDOW = int(os.getenv("HOUR_WINDOW", "1"))      # only post items within this many hours
TIMEZONE = os.getenv("TIMEZONE", "UTC")               # e.g., "America/Chicago"

# Visibility / testing
POST_ON_STARTUP = os.getenv("POST_ON_STARTUP", "false").lower() == "true"
POST_STARTUP_MAX = int(os.getenv("POST_STARTUP_MAX", "1"))

# Feeds & filters
DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://beincrypto.com/feed/",
    "https://u.today/rss",
    "https://ambcrypto.com/feed/",
    "https://news.bitcoin.com/feed/",
    # Optional broad aggregator (very active). Uncomment to use:
    # "https://news.google.com/rss/search?q=crypto+OR+cryptocurrency+OR+bitcoin+OR+ethereum&hl=en-US&gl=US&ceid=US:en",
]
FEEDS = [f.strip() for f in os.getenv("FEEDS", ",".join(DEFAULT_FEEDS)).split(",") if f.strip()]

# Leave KEYWORDS empty to accept ALL crypto headlines. Example to filter:
# KEYWORDS="bitcoin, ethereum, solana"
KEYWORDS = [k.strip() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
EXCLUDE_KEYWORDS = [k.strip() for k in os.getenv("EXCLUDE_KEYWORDS", "").split(",") if k.strip()]

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto-news-bot")

if not DISCORD_TOKEN or CHANNEL_ID == 0:
    raise SystemExit("Set DISCORD_TOKEN and CHANNEL_ID env vars.")

# ---------------- Discord ----------------
INTENTS = discord.Intents.default()
client = discord.Client(intents=INTENTS)

# De-dupe (in-memory for this run)
SEEN_IDS: Set[str] = set()
HAS_POSTED_ON_STARTUP = False


# ---------------- Helpers ----------------
def _strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    text = re.sub(r"(?s)<.*?>", "", html)
    text = ihtml.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    return text


def _first_sentences(text: str, max_chars: int, max_sents: int = 6) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text)
    out, total = [], 0
    for p in parts:
        p = p.strip()
        if not p:
            continue
        n = len(p) + (1 if out else 0)
        if total + n > max_chars:
            break
        out.append(p)
        total += n
        if len(out) >= max_sents:
            break
    joined = " ".join(out).strip()
    return joined or text[:max_chars].rstrip()


def _entry_id(entry: dict) -> str:
    return (
        entry.get("id")
        or entry.get("guid")
        or entry.get("link")
        or f"{entry.get('title','')}-{entry.get('published','')}"
        or os.urandom(8).hex()
    )


def _match_topic(text: str) -> bool:
    if not KEYWORDS:  # empty = accept all
        return True
    t = text.lower()
    if EXCLUDE_KEYWORDS and any(ex.lower() in t for ex in EXCLUDE_KEYWORDS):
        return False
    return any(k.lower() in t for k in KEYWORDS)


def _parse_pubdate(pub_str: str, tz: ZoneInfo) -> Optional[datetime]:
    if not pub_str:
        return None
    try:
        dt = dateutil.parser.parse(pub_str)
        if dt.tzinfo is None:
            # Assume already in target tz if naive; treat as tz-aware in that zone
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except Exception:
        return None


async def _http_text(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Fetch text; gracefully skip 404 or other non-200s."""
    if not url:
        return None
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 404:
                log.warning("Page 404, skipping: %s", url)
                return None
            if resp.status != 200:
                log.warning("Page HTTP %s for %s", resp.status, url)
                return None
            return await resp.text()
    except Exception as e:
        log.warning("Page fetch error for %s: %s", url, e)
        return None


async def get_long_synopsis(session: aiohttp.ClientSession, url: str, fallback: str, rss_summary: Optional[str]) -> str:
    """Long synopsis = RSS summary + meta description + first paragraphs."""
    pieces: List[str] = []
    if rss_summary:
        pieces.append(_strip_tags(rss_summary).strip())

    html = await _http_text(session, url)
    if html:
        def grab(content: str, needle: str) -> Optional[str]:
            c_low = content.lower()
            i = c_low.find(needle)
            if i == -1:
                return None
            j = c_low.find('content="', i)
            if j == -1:
                return None
            j += len('content="')
            k = content.find('"', j)
            if k == -1:
                return None
            return content[j:k].strip()

        og = grab(html, 'property="og:description"')
        tw = grab(html, 'name="twitter:description"')
        md = grab(html, 'name="description"')
        meta = og or tw or md
        if meta:
            pieces.append(meta.strip())

        article_match = re.search(r"(?is)<article[^>]*>(.*?)</article>", html)
        body_html = article_match.group(1) if article_match else html
        paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body_html)
        body_text = _strip_tags("\n\n".join(paragraphs[:8]) if paragraphs else body_html)
        if body_text:
            pieces.append(_first_sentences(body_text, SYNOPSIS_MAX_CHARS * 2, max_sents=6))

    synopsis = " ".join([p for p in pieces if p]).strip() or fallback
    if len(synopsis) > SYNOPSIS_MAX_CHARS:
        synopsis = synopsis[:SYNOPSIS_MAX_CHARS].rstrip() + "…"
    return synopsis


# ---------------- Feeds ----------------
async def parse_feed(session: aiohttp.ClientSession, feed_url: str) -> List[Dict]:
    out: List[Dict] = []
    try:
        async with session.get(feed_url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 404:
                log.warning("Removing dead feed (404): %s", feed_url)
                return out
            if resp.status != 200:
                log.warning("Feed HTTP %s: %s", resp.status, feed_url)
                return out
            content = await resp.read()
    except Exception as e:
        log.warning("Feed error %s: %s", feed_url, e)
        return out

    parsed = feedparser.parse(content)
    feed_title = parsed.feed.get("title", "") if parsed.feed else ""

    for entry in parsed.entries:
        title = entry.get("title", "") or ""
        summary = entry.get("summary", "") or entry.get("description", "") or ""
        link = entry.get("link", "") or ""
        combined = " ".join([title, summary, link])

        if not _match_topic(combined):
            continue

        item = {
            "id": _entry_id(entry),
            "title": title or "New article",
            "summary": summary,
            "link": link,
            "published": entry.get("published", "") or entry.get("updated", ""),
            "source": feed_title,
        }
        out.append(item)
    return out


async def poll_all_feeds(session: aiohttp.ClientSession) -> List[Dict]:
    tasks = [parse_feed(session, url) for url in FEEDS]
    results: List[List[Dict]] = await asyncio.gather(*tasks, return_exceptions=True)
    items: List[Dict] = []
    for res in results:
        if isinstance(res, list):
            items.extend(res)

    # Sort newest first when possible
    def _key(x: Dict):
        return x.get("published") or ""
    items.sort(key=_key, reverse=True)
    return items


async def build_message(item: Dict, session: aiohttp.ClientSession) -> Tuple[str, Optional[discord.Embed]]:
    title = item.get("title") or "New article"
    url = item.get("link") or ""
    source = item.get("source") or "Source"
    published = item.get("published") or ""
    rss_summary = item.get("summary")

    synopsis = await get_long_synopsis(session, url, fallback=title, rss_summary=rss_summary)

    text = f"**{title}**\n{url}\n\n**Synopsis:** {synopsis}"
    filter_desc = ("Filtering OFF (all crypto news)" if not KEYWORDS
                   else f"Keywords: {', '.join(KEYWORDS)}")
    embed = discord.Embed(
        title=f"{source} • {published}",
        description=filter_desc,
        url=url,
    )
    return text, embed


# ---------------- Poller ----------------
@tasks.loop(minutes=POLL_MINUTES, reconnect=True)
async def poll_and_post():
    """Poll feeds and only post items from *today* within the last HOUR_WINDOW hours (in TIMEZONE)."""
    global HAS_POSTED_ON_STARTUP

    await client.wait_until_ready()

    # Ensure the channel is available
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except Exception as e:
            log.error("Cannot fetch channel %s: %s", CHANNEL_ID, e)
            return

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    cutoff = now - timedelta(hours=HOUR_WINDOW)
    today = now.date()

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT + 5)
    connector = aiohttp.TCPConnector(limit=15, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        items = await poll_all_feeds(session)
        if not items:
            log.info("No entries found this cycle.")
            return

        # Apply time filter (today + within HOUR_WINDOW)
        recent_items: List[Dict] = []
        for it in items:
            pub_dt = _parse_pubdate(it.get("published", ""), tz)
            if not pub_dt:
                continue
            if pub_dt.date() != today:
                continue
            if pub_dt < cutoff:
                continue
            recent_items.append(it)

        if not recent_items:
            log.info("No posts from today within the last %d hour(s).", HOUR_WINDOW)
            return

        posted = 0
        for item in recent_items:
            iid = item.get("id", "")

            should_post = False
            if POST_ON_STARTUP and not HAS_POSTED_ON_STARTUP:
                should_post = True
            elif iid and iid not in SEEN_IDS:
                should_post = True

            if not should_post:
                continue

            text, embed = await build_message(item, session)
            try:
                await channel.send(text, embed=embed)
                if iid:
                    SEEN_IDS.add(iid)
                posted += 1
                HAS_POSTED_ON_STARTUP = True
                log.info("Posted (recent): %s | %s", item.get("source"), (item.get("title") or "")[:80])
            except Exception as e:
                log.exception("Failed sending message: %s", e)

            if POST_ON_STARTUP and posted >= POST_STARTUP_MAX:
                break

        if posted == 0:
            # Useful diagnostics
            newest = items[0]
            log.info("No new eligible posts this cycle (time- or dedupe-filtered). Latest seen: %s | %s",
                     newest.get("source"), (newest.get("title") or "")[:80])


@poll_and_post.before_loop
async def before_poll():
    await client.wait_until_ready()


@client.event
async def on_ready():
    log.info("Logged in as %s (%s)", client.user, client.user.id)
    if not poll_and_post.is_running():
        poll_and_post.start()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return
    cmd = message.content.strip().lower()
    if cmd == "!newsnow":
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        cutoff = now - timedelta(hours=HOUR_WINDOW)
        today = now.date()

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT + 5)
        connector = aiohttp.TCPConnector(limit=15, ttl_dns_cache=300)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            items = await poll_all_feeds(session)
            # filter to current day/hour window for manual trigger too
            recent = []
            for it in items:
                pub_dt = _parse_pubdate(it.get("published", ""), tz)
                if not pub_dt:
                    continue
                if pub_dt.date() != today or pub_dt < cutoff:
                    continue
                recent.append(it)

            if not recent:
                await message.channel.send(f"No crypto headlines from the past {HOUR_WINDOW} hour(s).")
                return

            # Prefer first not-seen; else newest recent
            chosen = next((it for it in recent if it.get("id") not in SEEN_IDS), recent[0])
            text, embed = await build_message(chosen, session)
            await message.channel.send(text, embed=embed)
            if chosen.get("id"):
                SEEN_IDS.add(chosen["id"])


def main():
    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
