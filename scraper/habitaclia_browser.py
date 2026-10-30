"""Habitaclia, read from its own search pages with a real browser.

Habitaclia's alert emails are summaries. "1.772 novedades en comarca
Barcelonès" arrives with four flats in it, and nothing on this side can
recover the other 1.768: they never reach the mailbox. Its search page, by
contrast, lists every flat in the search, thirty-odd to a page.

The page is built by JavaScript. Fetched plainly it carries prices but no
links, so a plain HTTP scraper — the old one in habitaclia.py — finds
nothing; it also still looks for the "i12345678.htm" addresses the site
dropped when it moved onto Fotocasa's platform. A headless browser sees
what a person sees: one <article> per flat, the title linking to
/comprar/<tipo>/.../<uuid>/d.

The cards say more than the emails did: floor, bathrooms and a price cut
are on them, where the email had at most price, size and rooms.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

BASE = "https://www.habitaclia.com"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_UUID_RE = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/d\b")
_PRICE_RE = re.compile(r"^([\d.]{4,11})\s*€$")
_DROP_RE = re.compile(r"[Hh]a bajado\s+([\d.]+)\s*€")
_AREA_RE = re.compile(r"(\d{2,4})\s*m²")
_ROOMS_RE = re.compile(r"(\d{1,2})\s*hab")
_BATHS_RE = re.compile(r"(\d{1,2})\s*baños?")
_FLOOR_RE = re.compile(r"^(\d{1,2})º$|^(bajo|entresuelo|principal|ático|sótano)$", re.I)
_TITLE_RE = re.compile(r"\ben venta en\b", re.I)


def _int(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text.replace(".", ""))
    except ValueError:
        return None


def parse_card(text: str, href: str) -> dict | None:
    """One search-result card, as the page renders it, into a listing.

    `text` is the card's innerText — one fact per line, for example:

        1/32
        515.000 €
        Ha bajado 10.000 €
        Piso con ascensor en venta en CL LAS NAVAS DE TOLOSA
        Sant Andreu, Barcelona Capital
        70 m²
        2 hab.
        2 baños
        7º

    Returns None for anything that is not a priced flat with an id: the
    page also carries promoted blocks and agency banners.
    """
    m = _UUID_RE.search(href or "")
    if not m:
        return None

    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    price = None
    for ln in lines:
        pm = _PRICE_RE.match(ln)
        if pm:
            price = _int(pm.group(1))
            break
    if not price or price < 10_000:
        return None

    title = next((ln for ln in lines if _TITLE_RE.search(ln)), "")
    # The line under the title is where it is: "Sant Andreu, Barcelona
    # Capital" — district then town — or the town alone.
    location = ""
    if title:
        i = lines.index(title)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt and not _AREA_RE.search(nxt) and not _ROOMS_RE.search(nxt):
            location = nxt

    district, city = None, None
    if location:
        parts = [p.strip() for p in location.split(",") if p.strip()]
        if len(parts) >= 2:
            district, city = parts[0], parts[-1]
        elif parts:
            city = parts[0]
    if city:
        city = re.sub(r"\s+capital$", "", city, flags=re.I).strip()

    blob = "\n".join(lines)
    area = _AREA_RE.search(blob)
    rooms = _ROOMS_RE.search(blob)
    baths = _BATHS_RE.search(blob)
    drop = _DROP_RE.search(blob)

    floor = None
    for ln in lines:
        fm = _FLOOR_RE.match(ln)
        if fm:
            floor = (f"{fm.group(1)}ª planta" if fm.group(1) else fm.group(2).lower())
            break

    return {
        "portal": "habitaclia",
        "external_id": m.group(1),
        "url": BASE + href.split("?")[0],
        "title": title,
        "price": float(price),
        "area_m2": float(area.group(1)) if area else None,
        "rooms": int(rooms.group(1)) if rooms else None,
        "bathrooms": int(baths.group(1)) if baths else None,
        "floor": floor,
        "district": district.lower() if district else None,
        "city": city.lower() if city else None,
        "price_drop": _int(drop.group(1)) if drop else None,
        "description": blob[:600],
    }


def _executable_path() -> str | None:
    """A Chromium already on disk, when the installed playwright expects
    another build. The CI runner installs its own; elsewhere it may not."""
    explicit = os.environ.get("CHROMIUM_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    return None


def scrape(url: str, max_pages: int = 3, timeout_ms: int = 90_000,
           city: str | None = None, district: str | None = None,
           known=None) -> list[dict]:
    """Every flat on the first `max_pages` pages of one search.

    `city` and `district` come from the search's own configuration and
    override what the card says. The card's location line is ambiguous:
    "Sant Andreu, Barcelona Capital" is district then town, but "Manresa,
    Barcelona" is town then province — read the same way, every Manresa
    flat was filed under Barcelona and priced against Barcelona's rents.
    The search knows where it is looking; the card only half does.

    Pagination is "/s/2", "/s/3" on the canonical search address; the
    first page redirects there from the old ".htm" form, so the page's own
    final URL is used to build the rest. Stops early when a page adds
    nothing new — the last page is reached or the site repeated itself.

    `known`, given a page's ids, says whether every one of them was seen
    before. Read newest first, a page of nothing but known flats means the
    news is above it, so the next page is not worth the wait: a pass that
    reads one page per search instead of three comes round three times as
    often.
    """
    from playwright.sync_api import sync_playwright

    found: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_executable_path(),
                                    args=["--no-sandbox"])
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="es-ES",
                                    viewport={"width": 1400, "height": 2400})
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            canonical = page.url.split("?")[0]
            query = ("?" + url.split("?", 1)[1]) if "?" in url else ""
            if query and "?" not in page.url:
                # The old ".htm" form redirects to the canonical address and
                # drops the filters on the way; reload with them reattached.
                page.goto(canonical + query, wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_timeout(1200)

            for n in range(1, max_pages + 1):
                if n > 1:
                    page.goto(f"{canonical.rstrip('/')}/{n}{query}",
                              wait_until="networkidle", timeout=timeout_ms)
                    page.wait_for_timeout(1200)
                cards = page.eval_on_selector_all(
                    "article",
                    """els => els.map(e => {
                        const a = e.querySelector('h3 a[href^="/comprar/"]')
                               || e.querySelector('a[href^="/comprar/"]');
                        return {href: a ? a.getAttribute('href') : '',
                                text: e.innerText || ''};
                    })""",
                )
                before = len(found)
                page_ids = []
                for c in cards:
                    listing = parse_card(c["text"], c["href"])
                    if not listing:
                        continue
                    if city:
                        listing["city"] = city.lower()
                    if district:
                        listing["district"] = district.lower()
                    found.setdefault(listing["external_id"], listing)
                    page_ids.append(listing["external_id"])
                logger.info(f"Habitaclia navegador: {url[-60:]} página {n} → "
                            f"{len(cards)} tarjetas, {len(found) - before} pisos nuevos")
                if len(found) == before:
                    break
                if known is not None and page_ids and known(page_ids):
                    break
        finally:
            browser.close()
    return list(found.values())
