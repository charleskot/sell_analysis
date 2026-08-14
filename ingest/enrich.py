"""Read a listing's own page to recover what the alert email left out.

Alert emails are summaries. Many carry price, m² and rooms and nothing
else — 58% of listings never state their condition — so a flat advertised
as "para reformar" arrives looking identical to one ready to move into.
That single missing word is worth tens of thousands of euros in works the
bank will not lend for.

This runs only for listings that already matched a profile: a handful a
day, not the whole feed. Failure is expected and harmless — Idealista and
Fotocasa refuse datacenter IPs, so their pages stay unreadable from a
scheduled runner, and the listing simply keeps its "condition unknown"
flag rather than gaining a false one.
"""
import logging
import re

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20

# A real browser's headers. Not evasion — these portals serve a different
# page to clients that look automated, and the goal is the page a buyer sees.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,ca;q=0.8",
}

# Where each portal keeps the descriptive text, tried after the meta tag.
# Ordered most to least specific — a loose selector matches the page's
# advice sidebar, which is identical on every listing and mentions nothing
# about the flat.
_DESCRIPTION_SELECTORS = [
    ".desctext", ".primer-bloque",                          # Habitaclia
    ".detail-description", ".comment", "#details",          # Idealista
    ".fc-detail-text", "[class*='DetailDescription']",      # Fotocasa
    "[itemprop='description']",
]

_MAX_CHARS = 4000


def fetch_description(url: str) -> str | None:
    """Return the listing's description text, or None if unreachable.

    None means "could not read", never "the flat has no description" — the
    caller must keep treating condition as unknown in that case.
    """
    if not url or not url.startswith("http"):
        return None

    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as e:
        logger.warning(f"enrich: missing dependency ({e})")
        return None

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT_SECONDS, allow_redirects=True)
    except Exception as e:
        logger.info(f"enrich: request failed for {url[:60]}: {e}")
        return None

    if resp.status_code != 200:
        # 403/405 is the portal refusing this IP, not a broken listing
        logger.info(f"enrich: {resp.status_code} from {url[:60]}")
        return None

    # An anti-bot interstitial returns 200 with a tiny body
    if len(resp.text) < 20_000:
        logger.info(f"enrich: page too small ({len(resp.text)}b), probably a block page")
        return None

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.info(f"enrich: parse failed: {e}")
        return None

    # The meta description is tried first and works across portals: every one
    # of them fills it from the advert itself. It is also immune to layout
    # changes, unlike a CSS selector, and short enough to carry no furniture.
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content") and len(meta["content"]) > 60:
        return re.sub(r"\s+", " ", meta["content"])[:_MAX_CHARS]

    for selector in _DESCRIPTION_SELECTORS:
        for el in soup.select(selector):
            text = el.get_text(" ", strip=True)
            if len(text) > 80:
                return re.sub(r"\s+", " ", text)[:_MAX_CHARS]

    logger.info(f"enrich: no description found in {url[:60]}")
    return None
