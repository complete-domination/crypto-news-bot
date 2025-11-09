import re
import html as ihtml

SYNOPSIS_MAX_CHARS = int(os.getenv("SYNOPSIS_MAX_CHARS", "800"))

def _strip_tags(html: str) -> str:
    # remove scripts/styles
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    # replace <br> and </p> with newlines
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    # strip all tags
    text = re.sub(r"(?s)<.*?>", "", html)
    # unescape & cleanup whitespace
    text = ihtml.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    return text

def _first_sentences(text: str, max_chars: int) -> str:
    # aim for a few sentences up to max_chars
    # split on sentence enders; keep reasonably compact
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    total = 0
    for p in parts:
        if not p.strip():
            continue
        if total + len(p) + (1 if out else 0) > max_chars:
            break
        out.append(p.strip())
        total += len(p) + (1 if out else 0)
        if len(out) >= 4:  # cap at ~3–4 sentences
            break
    joined = " ".join(out).strip()
    if not joined:
        joined = text[:max_chars].rstrip()
    return joined

async def try_get_synopsis(session: aiohttp.ClientSession, article_url: str, fallback_title: str) -> str:
    """
    Longer synopsis:
    1) pull OG/Twitter/meta description
    2) append first paragraphs from the article body (heuristic)
    3) trim to SYNOPSIS_MAX_CHARS
    """
    # step 1: fetch page
    try:
        async with session.get(article_url, timeout=25) as resp:
            if resp.status != 200:
                return fallback_title
            html = await resp.text()
    except Exception:
        return fallback_title

    # step 2: harvest meta description-like fields (keep from the earlier helper)
    def _grab(content: str, needle: str):
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
    meta = og or tw or md

    # step 3: extract main paragraphs (very light heuristic)
    # prefer content inside <article>, fallback to whole page
    article_match = re.search(r"(?is)<article[^>]*>(.*?)</article>", html)
    body_html = article_match.group(1) if article_match else html
    # pull first few <p> blocks
    paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body_html)
    body_text = _strip_tags("\n\n".join(paragraphs[:6]) if paragraphs else body_html)

    # combine meta + body
    pieces = []
    if meta:
        pieces.append(meta.strip())
    if body_text:
        # collapse to a few sentences, avoid duplicating the meta line
        main = _first_sentences(body_text, SYNOPSIS_MAX_CHARS * 2)  # temp long, trim later
        if meta and main.startswith(meta[:80]):  # crude duplicate guard
            main = main[len(meta):].lstrip()
        if main:
            pieces.append(main)

    combo = " ".join(pieces).strip() if pieces else fallback_title
    # final trim
    if len(combo) > SYNOPSIS_MAX_CHARS:
        combo = combo[:SYNOPSIS_MAX_CHARS].rstrip() + "…"
    return combo
