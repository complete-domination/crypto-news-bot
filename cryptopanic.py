import os
import asyncio
import logging
from typing import Optional, Tuple

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

# ---- Env / Config ----
load_dotenv()  # local dev; in Railway we use Variables UI instead

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "2"))  # you can tweak cadence in Railway vars

if not DISCORD_TOKEN or not CRYPTOPANIC_TOKEN or CHANNEL_ID == 0:
    raise SystemExit("Set DISCORD_TOKEN, CRYPTOPANIC_TOKEN and CHANNEL_ID as env vars.")

# ---- Logging (Railway logs) ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cryptopanic-bot")

# ---- Discord client ----
INTENTS = discord.Intents.default()  # no privileged intents required for posting
client = discord.Client(intents=INTENTS)

LAST_SEEN_ID: Optional[str] = None  # simple dedupe


async def fetch_latest_post(session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Get newest CryptoPanic post.
    Narrow to public news (not discussions) for consistency.
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


async def try_get_synopsis(session: aiohttp.ClientSession, article_url: str, fallback_title: str) -> str:
    """
    Pull a short synopsis from common meta tags.
    Keeps deps light (no bs4); robust enough for many sites.
    """
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
    title = post.get("title") or post.get("slug") or "New post"
    url = post.get("url") or (post.get("source") or {}).get("url") or "https://cryptopanic.com/"
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


# ---- Poller (uses discord.ext.tasks, plays nice on Railway) ----
@tasks.loop(minutes=POLL_MINUTES, reconnect=True)
async def poll_and_post():
    global LAST_SEEN_ID
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("Channel %s not found. Is the bot in the server with correct perms?", CHANNEL_ID)
        return

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        post = await fetch_latest_post(session)
        if not post:
            return

        post_id = str(post.get("id", ""))
        if not post_id or post_id == LAST_SEEN_ID:
            return

        text, embed = await build_message(post, session)
        try:
            await channel.send(text, embed=embed)
            LAST_SEEN_ID = post_id
            log.info("Posted %s", post_id)
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


# Optional manual trigger
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
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
    # Ensure clean shutdown handling on Railway
    try:
        client.run(DISCORD_TOKEN, log_handler=None)  # we already configured logging
    except KeyboardInterrupt:
        log.info("Shutting down (KeyboardInterrupt).")


if __name__ == "__main__":
    main()
