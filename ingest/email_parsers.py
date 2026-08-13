"""Parse portal alert emails into RawListing objects.

Design note: these parsers deliberately do NOT depend on CSS classes or
email layout — both change often and silently. They key off:

  1. The listing URL pattern (stable for years per portal)
  2. Spanish text patterns for price / area / rooms (stable)

Adding a new portal = one entry in PORTAL_SPECS. No new code.
"""
import logging
import re
from html import unescape
from urllib.parse import urlparse, urlunparse

from scraper.base import RawListing

logger = logging.getLogger(__name__)


# ── Portal registry ──────────────────────────────────────────────────────
# sender_match: substring matched against the From address
# url_re:       finds listing URLs in the email body; group(1) = external id
PORTAL_SPECS = [
    {
        "portal": "idealista",
        "sender_match": ["idealista.com", "idealista.es"],
        "url_re": re.compile(r"https?://(?:www\.)?idealista\.com/(?:\w+/)?inmueble/(\d+)/?", re.I),
    },
    {
        "portal": "fotocasa",
        "sender_match": ["fotocasa.es", "fotocasa.com"],
        "url_re": re.compile(r"https?://(?:www\.)?fotocasa\.es/[^\s\"'<>]*?(\d{6,})(?:[/?#]|$)", re.I),
    },
    {
        "portal": "habitaclia",
        "sender_match": ["habitaclia.com"],
        # Habitaclia ids are suffixed as "-i12345678.htm"; some older links
        # omit the "i", so it is optional.
        "url_re": re.compile(r"https?://(?:www\.)?habitaclia\.com/[^\s\"'<>]*?-i?(\d{6,})\.htm", re.I),
    },
    {
        "portal": "servihabitat",
        "sender_match": ["servihabitat.com", "servihabitat.es"],
        "url_re": re.compile(r"https?://(?:www\.)?servihabitat\.com/[^\s\"'<>]*?(\d{5,})", re.I),
    },
    {
        "portal": "solvia",
        "sender_match": ["solvia.es"],
        "url_re": re.compile(r"https?://(?:www\.)?solvia\.es/[^\s\"'<>]*?(\d{5,})", re.I),
    },
    {
        "portal": "altamira",
        "sender_match": ["altamirainmuebles.com", "altamira"],
        "url_re": re.compile(r"https?://(?:www\.)?altamirainmuebles\.com/[^\s\"'<>]*?(\d{5,})", re.I),
    },
    {
        "portal": "aliseda",
        "sender_match": ["aliseda.es", "alisedainmobiliaria"],
        "url_re": re.compile(r"https?://(?:www\.)?aliseda\.es/[^\s\"'<>]*?(\d{5,})", re.I),
    },
]


# ── Field patterns (Spanish, portal-agnostic) ────────────────────────────

_PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+|\d{5,7})\s*€", re.I)
_AREA_RE = re.compile(r"(\d{2,4})\s*m(?:2|²|\^2)\b", re.I)
_ROOMS_RE = re.compile(r"(\d{1,2})\s*(?:hab|dorm|habitaci|dormitori)\w*", re.I)
_BATHS_RE = re.compile(r"(\d{1,2})\s*(?:baño|bany|aseo)\w*", re.I)
_FLOOR_RE = re.compile(
    r"(?:planta\s*)?(\d{1,2})\s*[ªº°]|"
    r"\b(bajo|entresuelo|principal|[áa]tico|sobre[áa]tico)\b",
    re.I,
)

# Rejection patterns — these never become opportunities
_REJECT_RE = re.compile(
    r"\balquilad[oa]\b|\bcon\s+inquilino\b|\barrendad[oa]\b|"
    r"\bocupad[oa]\b|\bokupad[oa]\b|\bokupas?\b|\busurpaci[oó]n\b|"
    r"\bnuda\s+propiedad\b|\bsubasta\b|\bproindiviso\b|"
    r"\bposesi[oó]n\s+no\s+garantizada\b|\bsin\s+posesi[oó]n\b",
    re.I,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[\s ]+")


def strip_html(html: str) -> str:
    """HTML -> readable text, preserving block boundaries as separators."""
    if not html:
        return ""
    # Turn block-level tags into separators so fields don't run together
    text = re.sub(r"(?i)<(?:br|/p|/div|/td|/tr|/li|/h[1-6])[^>]*>", " | ", html)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    return _WS_RE.sub(" ", text).strip()


def parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(" ", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    # Filter out obvious non-prices (rent figures, fees)
    return val if 20_000 <= val <= 3_000_000 else None


def parse_area(text: str) -> float | None:
    m = _AREA_RE.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if 15 <= val <= 1_000 else None


def parse_rooms(text: str) -> int | None:
    m = _ROOMS_RE.search(text)
    if not m:
        return None
    try:
        val = int(m.group(1))
    except ValueError:
        return None
    return val if 0 <= val <= 15 else None


def parse_baths(text: str) -> int | None:
    m = _BATHS_RE.search(text)
    if not m:
        return None
    try:
        val = int(m.group(1))
    except ValueError:
        return None
    return val if 0 <= val <= 10 else None


def parse_floor(text: str) -> str | None:
    m = _FLOOR_RE.search(text)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def clean_url(url: str) -> str:
    """Strip tracking query params — they break dedup across emails."""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return url


def detect_portal(sender: str, body: str) -> dict | None:
    """Match by sender first (reliable), fall back to body URL sniffing."""
    sender = (sender or "").lower()
    for spec in PORTAL_SPECS:
        if any(s in sender for s in spec["sender_match"]):
            return spec
    # Forwarded emails lose the original sender — sniff the body
    for spec in PORTAL_SPECS:
        if spec["url_re"].search(body or ""):
            return spec
    return None


def _split_blocks(body: str, match_spans: list[tuple[int, int]]) -> list[str]:
    """Slice the RAW body into one chunk per listing, then flatten each.

    Alert emails repeat a block per property. Slicing the raw body (not the
    stripped text) is what makes this work: listing URLs usually live only in
    href attributes, which the text flattening throws away.

    Each chunk runs from its own URL match to the start of the next one, so
    the price/m²/rooms that follow a link belong to that link.
    """
    if not match_spans:
        return []

    blocks = []
    for i, (start, _end) in enumerate(match_spans):
        stop = match_spans[i + 1][0] if i + 1 < len(match_spans) else len(body)
        blocks.append(strip_html(body[start:stop]))
    return blocks


def parse_email(sender: str, subject: str, html: str, text: str = "") -> list[RawListing]:
    """Extract listings from one alert email. Returns [] if unrecognised."""
    body = html or text
    if not body:
        return []

    spec = detect_portal(sender, body)
    if not spec:
        logger.debug(f"Email ingest: unrecognised sender {sender!r}, skipping")
        return []

    portal = spec["portal"]

    # Collect listing URLs in document order, de-duplicated by listing id.
    # Keep the LAST occurrence of each id: alert emails link a listing first
    # from its thumbnail (no data around it) and again from its title, with
    # price/m²/rooms following. The later block is the one carrying the data.
    by_id: dict[str, tuple[str, tuple[int, int]]] = {}
    for m in spec["url_re"].finditer(body):
        by_id[m.group(1)] = (clean_url(m.group(0)), m.span())

    if not by_id:
        logger.debug(f"Email ingest: no {portal} listing URLs in email {subject!r}")
        return []

    ordered = sorted(
        ((ext_id, url, span) for ext_id, (url, span) in by_id.items()),
        key=lambda t: t[2][0],
    )
    blocks = _split_blocks(body, [span for _, _, span in ordered])
    ordered = [(ext_id, url) for ext_id, url, _ in ordered]
    flat = strip_html(body)

    listings: list[RawListing] = []
    for i, (ext_id, url) in enumerate(ordered):
        block = blocks[i] if i < len(blocks) else flat

        price = parse_price(block)
        area = parse_area(block)
        rooms = parse_rooms(block)

        # A block with no price is almost always a header/footer false positive
        if price is None:
            continue

        # Hard reject: tenanted / occupied / auction / bare ownership
        if _REJECT_RE.search(block):
            logger.info(f"Email ingest: rejected {portal}/{ext_id} (ocupado/alquilado/subasta)")
            continue

        listings.append(
            RawListing(
                portal=portal,
                external_id=ext_id,
                url=url,
                title=(block[:180].strip() or subject[:180]),
                price=price,
                area_m2=area,
                rooms=rooms,
                bathrooms=parse_baths(block),
                floor=parse_floor(block),
                description=block[:600],
                city="barcelona",          # refined by location matching downstream
                district=None,
                published_ago_hours=0,     # an alert email IS the new-listing signal
            )
        )

    logger.info(f"Email ingest: {portal} → {len(listings)} listings from {subject[:60]!r}")
    return listings
