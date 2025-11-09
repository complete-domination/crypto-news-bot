import os
import re
import html as ihtml
import asyncio
import logging
from typing import Optional, Tuple, Set

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

# ---------------- Env / Config ----------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Tweakable knobs
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "2"))
SYNOPSIS_MAX_CHARS = int(os.getenv("SYNOPSIS_MAX_CHARS", "900"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))

# Visibility during testing
POST_ON_STARTUP = os.getenv("POST_ON_STARTUP", "false").lower() == "true"
POST_STARTUP_MAX = int(os.getenv("POST_STARTUP_MAX", "1"))

# Logging level
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cryptopanic-bot")

if not DISCORD_TOKEN or not CRYPTOPANIC_TOKEN or CHANNEL_ID == 0:
    raise SystemExit("Set DISCORD_TOKEN, CRYPTOPANIC_TOKEN, and CHANNEL_ID env vars.")

# ---------------- Discord ----------------
INTENTS = discord.Intents.default()
client = discord.Client(intents=INTENTS)

# De-dupe memory (this run)
SEEN_IDS: Set[str] = set()
HAS_POSTED_ON_STARTUP = False


# ---------------- Helpers ----------------
def _strip_tags(html: str) -> str:
    # remove scripts/styles
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    # replace line breaks
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    # strip remaining tags
    text = re.sub(r"(?s)<.*?>", "", html)
    # unescape & tidy
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


async def _http_text(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Fetch text with graceful handling of 404/other errors."""
    if not url:
        return None
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 404:
                # Skip this site/page (common request)
                log.warning("Synopsis skip (404): %s", url)
                return None
            if resp.status != 200:
                log.warning("Synopsis fetch HTTP %s for %s", resp.status, url)
                return None
            return await resp.text()
    except Exception as e:
        log.warning("Synopsis fetch error for %s: %s", url, e)
        return None


async def _long_synopsis(session: aiohttp.ClientSession, article_url: str, fallback_title: str,
                         rss_like_summary: Optional[str]) -> str:
    """
    Longer synopsis:
      1) start with any provided summary (CryptoPanic rarely includes one)
      2) add meta description (og/twitter/description) from the article
      3) add first paragraphs from <article>/<p> content
      4) trim to SYNOPSIS_MAX_CHARS
    """
    pieces = []
    if rss_like_summary:
        pieces.append(_strip_tags(rss_like_summary).strip())

    html = await _http_text(session, article_url)
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

        # Prefer content inside <article>, fallback to entire page
        article_match = re.search(r"(?is)<article[^>]*>(.*?)</article>", html)
        body_html = article_match.group(1) if article_match else html
        paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body_html)
        body_text = _strip_tags("\n\n".join(paragraphs[:8]) if paragraphs else body_html)
        if body_text:
            pieces.append(_first_sentences(body_text, SYNOPSIS_MAX_CHARS * 2, max_sents=6))

    combo = " ".join([p for p in pieces if p]).strip() or fallback_title
    if len(combo) > SYNOPSIS_MAX_CHARS:
        combo = combo[:SYNOPSIS_MAX_CHARS].rstrip() + "…"
    return combo


# ---------------- CryptoPanic API ----------------
async def fetch_latest_post(session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Fetch newest CryptoPanic news post.
    Docs: https://cryptopanic.com/developers/api/
    """
    url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_TOKEN}&public=true&kind=news"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("CryptoPanic HTTP %s", resp.status)
                return None
            data = await resp.json()
    except Exception as e:
        log.warning("CryptoPanic fetch error: %s", e)
        return None

    results = data.get("results", [])
    if not results:
        return None
    # Newest-first; take the first
    return results[0]


async def build_message(post: dict, session: aiohttp.ClientSession) -> Tuple[str, Optional[discord.Embed]]:
    title = post.get("title") or post.get("slug") or "New post"
    url = post.get("url") or (post.get("source") or {}).get("url") or "https://cryptopanic.com/"
    published_at = post.get("published_at", "")
    source = (post.get("source") or {}).get("domain", "Source")
    votes = post.get("votes") or {}
    bullish = votes.get("positive", 0)
    bearish = votes.get("negative", 0)

    # CryptoPanic usually doesn't supply a full text summary; pass None
    synopsis = await _long_synopsis(session, url, fallback_title=title, rss_like_summary=None)

    text = f"**{title}**\n{url}\n\n**Synopsis:** {synopsis}"
    embed = discord.Embed(
        title=f"{source} • {published_at}",
        description=f"Sentiment votes — 👍 {bullish}  |  👎 {bearish}",
        url=url,
    )
    return text, embed


# ---------------- Poller ----------------
@tasks.loop(minutes=POLL_MINUTES, reconnect=True)
async def poll_and_post():
    global HAS_POSTED_ON_STARTUP

    await client.wait_until_ready()

    # Channel lookup (cache-safe)
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except Exception as e:
            log.error("Cannot fetch channel %s: %s", CHANNEL_ID, e)
            return

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT + 5)
    connector = aiohttp.TCPConnector(limit=15, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        post = await fetch_latest_post(session)
        if not post:
            log.info("No results from CryptoPanic this cycle.")
            return

        pid = str(post.get("id") or "")
        newest_title = (post.get("title") or "")[:80]

        # Decide whether to post:
        should_post = False
        if POST_ON_STARTUP and not HAS_POSTED_ON_STARTUP:
            should_post = True
        elif pid and pid not in SEEN_IDS:
            should_post = True

        if not should_post:
            log.info("No new posts to send this cycle. (latest: %s)", newest_title)
            return

        text, embed = await build_message(post, session)
        try:
            await channel.send(text, embed=embed)
            if pid:
                SEEN_IDS.add(pid)
            HAS_POSTED_ON_STARTUP = True
            log.info("Posted: %s", newest_title)
        except Exception as e:
            log.exception("Failed sending message: %s", e)


@poll_and_post.before_loop
async def before_poll():
    await client.wait_until_ready()


@client.event
async def on_ready():
    log.info("Logged in as %s (%s)", client.user, client.user.id)
    if not poll_and_post.is_running():
        poll_and_post.start()


# Optional manual trigger in the target channel
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return
    if message.content.strip().lower() == "!newsnow":
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT + 5)
        connector = aiohttp.TCPConnector(limit=15, ttl_dns_cache=300)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            post = await fetch_latest_post(session)
            if not post:
                await message.channel.send("Couldn’t fetch news right now.")
                return
            pid = str(post.get("id") or "")
            text, embed = await build_message(post, session)
            await message.channel.send(text, embed=embed)
            if pid:
                SEEN_IDS.add(pid)


def main():
    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
