# cryptopanic.py
import os
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

# -------- Env / Config --------
load_dotenv()  # for local dev; Railway uses Variables UI

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "2"))  # adjust cadence in env

if not DISCORD_TOKEN or not CRYPTOPANIC_TOKEN or CHANNEL_ID == 0:
    raise SystemExit("Set DISCORD_TOKEN, CRYPTOPANIC_TOKEN and CHANNEL_ID env vars.")

# -------- Logging (shows in Railway logs) --------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cryptopanic-bot")

# -------- Discord client --------
INTENTS = discord.Intents.default()  # no privileged intents needed to post
client = discord.Client(intents=INTENTS)

# Dedupe memory (simple, in-process)
LAST_SEEN_ID: Optional[str] = None
LAST_SEEN_TITLE: Optional[str] = None


async def fetch_latest_post(session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Get the newest CryptoPanic post (news only, public).
    """
    url = (
        f"https://cryptopanic.com/api/v1/posts/"
        f"?auth_token={CRYPTOPANIC_TOKEN}&public=true&kind=news"
    )
    try:
        async with session.get(url, timeout=20) as resp:
            if resp.status != 200:
                log.warning("CryptoPanic HTTP %s", resp.status)
                return None
            data = await resp.json()
            results = data.get("results", [])
            return results[0] if results else None
    except Exception as e:
        log.exception("Error fetching latest post: %s", e)
        return None


def pick_best_url(post: dict) -> str:
    """
    Prefer the original article URL. Fall back sensibly.
    """
    post_id = str(post.get("id", "")) if post else ""
    return (
        (post or {}).get("news_url")                           # sometimes present
        or (post or {}).get("url")                             # sometimes present
        or ((post or {}).get("source") or {}).get("url")       # often present here
        or (f"https://cryptopanic.com/post/{post_id}/" if post_id else "https://cryptopanic.com/")
    )


async def try_get_synopsis(session: aiohttp.ClientSession, article_url: str, fallback_title: str) -> str:
    """
    Pull a short synopsis from common meta tags.
    Avoid scraping cryptopanic.com (generic description causes dupes).
    """
    try:
        host = urlparse(article_url).hostname or ""
    except Exception:
        host = ""

    # If we only have a CryptoPanic link, use a clean fallback.
    if "cryptopanic.com" in host:
        return fallback_title

    try:
        async with session.get(article_url, timeout=20) as resp:
            if resp.status != 200:
                return fallback_title
            html = await resp.text()
    except Exception:
        return fallback_title

    def _grab(content: str, needle: str) -> Optional[str]:
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

    og = _grab(html, 'property="og:description"')
    tw = _grab(html, 'name="twitter:description"')
    md = _grab(html, 'name="description"')

    summary = og or tw or md or fallback_title
    return (summary[:297].rstrip() + "...") if len(summary) > 300 else summary


async def build_message(post: dict, session: aiohttp.ClientSession) -> Tuple[str, Optional[discord.Embed]]:
    """
    Build the Discord message + embed.
    """
    title = post.get("title") or post.get("slug") or "New post"
    url = pick_best_url(post)
    published_at = post.get("published_at", "")
    domain = (post.get("source") or {}).get("domain", "Source")
    votes = post.get("votes") or {}
    bullish = votes.get("positive", 0)
    bearish = votes.get("negative", 0)

    synopsis = await try_get_synopsis(session, url, fallback_title=title)

    text = f"**{title}**\n{url}\n\n**Synopsis:** {synopsis}"
    embed = discord.Embed(
        title=f"{domain} • {published_at}",
        description=f"Sentiment votes — 👍 {bullish}  |  👎 {bearish}",
        url=url,
    )
    return text, embed


@tasks.loop(minutes=POLL_MINUTES, reconnect=True)
async def poll_and_post():
    """
    Periodically fetch and post latest item. Dedupe on id/title.
    """
    global LAST_SEEN_ID, LAST_SEEN_TITLE

    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("Channel %s not found. Invite the bot and check permissions.", CHANNEL_ID)
        return

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        post = await fetch_latest_post(session)
        if not post:
            return

        post_id = str(post.get("id", ""))
        post_title = post.get("title") or ""

        if (post_id and post_id == LAST_SEEN_ID) or (post_title and post_title == LAST_SEEN_TITLE):
            return  # no reposts

        text, embed = await build_message(post, session)
        try:
            await channel.send(text, embed=embed)
            LAST_SEEN_ID = post_id or LAST_SEEN_ID
            LAST_SEEN_TITLE = post_title or LAST_SEEN_TITLE
            log.info("Posted %s | %s", post_id, post_title[:60])
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


# Optional manual trigger, locked to the one channel
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return  # ignore everywhere else
    if message.content.strip().lower() == "!newsnow":
        timeout = aiohttp.ClientTimeout(total=30)
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            post = await fetch_latest_post(session)
            if not post:
                await message.channel.send("Couldn’t fetch news right now.")
                return
            text, embed = await build_message(post, session)
            await message.channel.send(text, embed=embed)


def main():
    try:
        # log_handler=None because we already configured logging above
        client.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
