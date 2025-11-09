# cryptopanic.py
import os
import re
import time
import json
import math
import asyncio
import logging
from typing import Optional, Tuple, Dict, Any, List

import aiohttp
import discord

# Optional extractors (pure Python)
try:
    import trafilatura  # best extractor
except Exception:
    trafilatura = None

try:
    from readability import Document  # fallback extractor
    from bs4 import BeautifulSoup
except Exception:
    Document = None
    BeautifulSoup = None

# -------- Config --------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("TOKEN")
CHANNEL_ID = int(os.environ["CHANNEL_ID"])  # required
CRYPTOPANIC_TOKEN = os.environ.get("CRYPTOPANIC_TOKEN")  # optional
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "90"))

if not DISCORD_TOKEN:
    raise SystemExit("Missing env var DISCORD_TOKEN")
if not CHANNEL_ID:
    raise SystemExit("Missing env var CHANNEL_ID")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("cryptopanic-bot")

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# memory to prevent repeats
last_seen_id: Optional[str] = None
last_synopsis: Optional[str] = None

# -------- Utilities --------
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    # drop trailing site boilerplate if obvious
    t = re.sub(r"Read more.*$", "", t, flags=re.IGNORECASE)
    return t

def split_sentences(text: str) -> List[str]:
    # lightweight sentence splitter
    text = clean_text(text)
    # protect abbreviations
    text = re.sub(r"(Mr|Ms|Dr|Prof|Jr|Sr|St)\.", r"\1<dot>", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(“\"'])", text)
    parts = [p.replace("<dot>", ".").strip() for p in parts if p.strip()]
    return parts

def summarize(text: str, max_sentences: int = 3, max_chars: int = 420) -> str:
    """
    Simple frequency-based extractive summary that prefers:
    - lead sentence
    - high keyword density sentences
    """
    sentences = split_sentences(text)
    if not sentences:
        return ""

    # if it's already short, return as-is
    joined = " ".join(sentences)
    if len(joined) <= max_chars and len(sentences) <= max_sentences:
        return joined

    # build a word freq table (lowercase, no tiny words)
    words = re.findall(r"[A-Za-z0-9$%\-\.]+", text.lower())
    stop = set(
        "the a an and or of to for from in on at as with by is are was were be has have had "
        "it that this those these you your their our its they he she them we not will would can "
        "could should may might about into over under after before amid among than".split()
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
        lead_bonus = 1.0 / (1 + i)  # prefer early sentences
        length_penalty = 1.0 if len(s) < 240 else 0.85
        return dens * 0.7 + lead_bonus * 0.25 + length_penalty * 0.05

    # rank sentences
    ranked = sorted(
        [(i, s, score(s, i)) for i, s in enumerate(sentences)],
        key=lambda x: x[2],
        reverse=True,
    )

    chosen = []
    used_idx = set()
    for i, s, _ in ranked:
        if len(chosen) >= max_sentences:
            break
        # discourage sentences next to each other to add variety
        if any(abs(i - j) <= 0 for j in used_idx):
            # allow adjacency if we don't have enough content
            pass
        chosen.append((i, s))
        used_idx.add(i)

    chosen.sort(key=lambda x: x[0])
    out = " ".join(s for _, s in chosen)
    # trim down if too long
    if len(out) > max_chars:
        out = out[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return out

async def fetch_json(session: aiohttp.ClientSession, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    for attempt in range(3):
        try:
            async with session.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=20) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as e:
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    return {}

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    for attempt in range(3):
        try:
            async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=25) as r:
                r.raise_for_status()
                return await r.text()
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    return ""

def meta_description(html: str) -> Optional[str]:
    if not BeautifulSoup:
        return None
    soup = BeautifulSoup(html, "lxml")
    for key in ["meta[name=description]", "meta[property='og:description']",
                "meta[name='twitter:description']"]:
        tag = soup.select_one(key)
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    # fall back to first paragraph
    p = soup.find("p")
    if p and p.text:
        return clean_text(p.text)
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

def extract_with_trafilatura(url: str, html: Optional[str] = None) -> Optional[str]:
    if not trafilatura:
        return None
    try:
        downloaded = html if html else trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return clean_text(text or "")
    except Exception:
        return None

async def get_latest_post(session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
    base = "https://cryptopanic.com/api/posts/"
    params = {"public": "true", "kind": "news", "regions": "en"}
    if CRYPTOPANIC_TOKEN:
        params["auth_token"] = CRYPTOPANIC_TOKEN
    data = await fetch_json(session, base, params)
    results = data.get("results") or []
    return results[0] if results else None

async def build_synopsis(session: aiohttp.ClientSession, url: str) -> str:
    # fetch html
    try:
        html = await fetch_text(session, url)
    except Exception as e:
        log.warning(f"Failed to fetch source page: {e}")
        return ""

    # try extractors
    text = None
    if trafilatura:
        text = extract_with_trafilatura(url, html)
    if not text:
        text = extract_with_readability(html)
    if not text:
        text = meta_description(html)

    if not text:
        return ""

    # clean and summarize
    text = re.sub(r"(©|Copyright).*?\d{4}.*", "", text, flags=re.IGNORECASE)
    text = clean_text(text)
    return summarize(text, max_sentences=3, max_chars=420)

def dedupe_synopsis(title: str, synopsis: str) -> str:
    """Avoid just repeating the headline."""
    if not synopsis:
        return ""
    t = re.sub(r"[^A-Za-z0-9 ]+", "", title.lower())
    s = re.sub(r"[^A-Za-z0-9 ]+", "", synopsis.lower())
    if t and t in s:
        # remove leading headline-like fragment
        s = re.sub(re.escape(title) + r"[:\-–]?\s*", "", synopsis, flags=re.IGNORECASE)
        s = s.strip()
    return s or synopsis

async def post_update(channel: discord.TextChannel, post: Dict[str, Any], synopsis: str):
    global last_synopsis
    title = post.get("title") or "Crypto News"
    link = post.get("url") or (post.get("source") or {}).get("url")
    votes = post.get("votes") or {}
    positive = votes.get("positive", 0)
    negative = votes.get("negative", 0)
    published_at = post.get("published_at") or post.get("created_at") or "unknown time"

    # avoid duplicate synopsis lines
    if synopsis and last_synopsis and synopsis.strip() == last_synopsis.strip():
        synopsis = ""

    embed = discord.Embed(
        title=title,
        url=link or discord.Embed.Empty,
        description=(f"**Synopsis:** {synopsis}" if synopsis else "_Synopsis unavailable_"),
        color=0x2b6cb0,
    )
    if link:
        embed.add_field(name="Source", value=f"[Open]({link}) • `{published_at}`", inline=False)
    embed.set_footer(text=f"Sentiment votes — 👍 {positive} | 👎 {negative}")

    await channel.send(embed=embed)
    if synopsis:
        last_synopsis = synopsis

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (id={client.user.id})")
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        raise SystemExit(f"Channel {CHANNEL_ID} not found")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                post = await get_latest_post(session)
                if not post:
                    log.info("No posts found.")
                else:
                    global last_seen_id
                    post_id = str(post.get("id"))
                    if post_id and post_id == last_seen_id:
                        log.debug("No new post.")
                    else:
                        last_seen_id = post_id
                        title = post.get("title", "")
                        link = post.get("url") or (post.get("source") or {}).get("url")
                        synopsis = ""
                        if link:
                            synopsis = await build_synopsis(session, link)
                            synopsis = dedupe_synopsis(title, synopsis)
                        await post_update(channel, post, synopsis)
        except Exception as e:
            log.exception(f"Loop error: {e}")
        await asyncio.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
