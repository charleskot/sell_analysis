"""Parse portal alert emails into RawListing objects.

Design note: these parsers deliberately do NOT depend on CSS classes or
email layout — both change often and silently. They key off:

  1. The listing URL pattern (stable for years per portal)
  2. Spanish text patterns for price / area / rooms (stable)

Adding a new portal = one entry in PORTAL_SPECS. No new code.
"""
import logging
import re
import urllib.parse
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
        # Two shapes, verified against real alert emails:
        #   /i34685004369863/28112891/express.../alertas/email/...  (alert redirect)
        #   /comprar-piso-el_clot-i9876543.htm                      (canonical)
        # The redirect form is what alert emails actually use.
        "url_re": re.compile(
            r"https?://(?:www\.)?habitaclia\.com/(?:i(\d{8,})/|[^\s\"'<>]*?-i?(\d{6,})\.htm)",
            re.I,
        ),
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
# Portals write the unit inconsistently: "78 m²", "78m2", and — in Habitaclia
# alert emails — "78m 2", where the superscript became a spaced digit.
_AREA_RE = re.compile(r"(\d{2,4})\s*m\s*(?:2|²|\^2)\b", re.I)
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


# Flattened blocks are pipe-separated. A usable title is a segment with real
# words in it — not a URL fragment, a bare figure, or leftover markup.
_TITLE_NOISE_RE = re.compile(
    r"https?://|target=|style=|utm_|^\W*$|^\s*[\d.,\s]+\s*(?:€|m\s*2|hab)?\s*$",
    re.I,
)

# "Piso en Barcelona - El Poble Sec - Paral·lel" / "Ático en Sabadell - Centre"
_LOCATION_RE = re.compile(
    r"\b(?:piso|[áa]tico|d[úu]plex|casa|chalet|apartamento|estudio|loft|masía|masia|"
    r"planta\s+baja|bajos?|adosad[oa]|paread[oa]|torre|finca)\s+en\s+([^|]+)",
    re.I,
)


def extract_title(block: str, fallback: str = "") -> str:
    """Pick the most title-like segment out of a flattened listing block."""
    for segment in (s.strip() for s in block.split("|")):
        if len(segment) < 12 or _TITLE_NOISE_RE.search(segment):
            continue
        # Needs enough letters to be prose rather than a spec line
        if sum(c.isalpha() for c in segment) < 8:
            continue
        return segment[:200]
    return fallback[:200]


def extract_location(title: str) -> tuple[str | None, str | None]:
    """Parse '<type> en <City> - <District> - <Area>' into (city, district).

    Getting this right matters: the scorer prices per zone, so a Maresme flat
    filed under Barcelona would be valued against the wrong comparables.
    """
    m = _LOCATION_RE.search(title or "")
    if not m:
        return None, None

    parts = [p.strip(" .-–") for p in m.group(1).split(" - ")]
    parts = [p for p in parts if p]
    if not parts:
        return None, None

    city = parts[0].lower() or None
    district = parts[1].lower() if len(parts) > 1 else None

    # Truncated segments ("Pa...") carry no information
    if district and (district.endswith("...") or len(district) < 3):
        district = None
    if city and city.endswith("..."):
        city = city.rstrip(". ") or None

    return city, district


def _match_id(match: re.Match) -> str | None:
    """First non-empty capture group.

    Portal patterns use alternation when a portal links listings in more than
    one shape, so the id can land in any group.
    """
    for group in match.groups():
        if group:
            return group
    return None


def unwrap_tracking(body: str) -> str:
    """Expose listing URLs hidden inside click-tracking wrappers.

    Portals route alert links through their own tracking domains, with the
    real destination percent-encoded in a query parameter:

        https://links.idealista.com/r/?u=https%3A%2F%2Fwww.idealista.com%2Finmueble%2F123%2F

    The listing regexes would never see that. Appending a decoded copy of the
    body makes them match without disturbing the original offsets, which the
    block splitting depends on.

    Decoding is applied repeatedly because some wrappers double-encode.
    """
    if not body:
        return body

    decoded = body
    for _ in range(3):
        prev = decoded
        decoded = urllib.parse.unquote(decoded)
        if decoded == prev:
            break

    return body if decoded == body else f"{body}\n{decoded}"


def detect_portal(sender: str, body: str) -> dict | None:
    """Match by sender first (reliable), fall back to body URL sniffing."""
    sender = (sender or "").lower()
    for spec in PORTAL_SPECS:
        if any(s in sender for s in spec["sender_match"]):
            return spec
    # Forwarded emails lose the original sender — sniff the body
    searchable = unwrap_tracking(body or "")
    for spec in PORTAL_SPECS:
        if spec["url_re"].search(searchable):
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

    # Tracking-wrapped links only become visible once decoded. The decoded
    # copy is appended, so offsets into the original half stay valid.
    search_body = unwrap_tracking(body)

    # Collect listing URLs in document order, de-duplicated by listing id.
    # Keep the LAST occurrence of each id: alert emails link a listing first
    # from its thumbnail (no data around it) and again from its title, with
    # price/m²/rooms following. The later block is the one carrying the data.
    by_id: dict[str, tuple[str, tuple[int, int]]] = {}
    for m in spec["url_re"].finditer(search_body):
        ext_id = _match_id(m)
        if ext_id:
            by_id[ext_id] = (clean_url(m.group(0)), m.span())

    if not by_id:
        logger.warning(
            f"Email ingest: {portal} email {subject[:60]!r} matched the sender but "
            "produced no listing URLs — the link format may have changed."
        )
        return []

    ordered = sorted(
        ((ext_id, url, span) for ext_id, (url, span) in by_id.items()),
        key=lambda t: t[2][0],
    )
    # Split the same string the spans were measured against — matches found
    # in the appended decoded half would otherwise slice out of bounds.
    blocks = _split_blocks(search_body, [span for _, _, span in ordered])
    ordered = [(ext_id, url) for ext_id, url, _ in ordered]
    flat = strip_html(search_body)

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

        title = extract_title(block, subject)
        city, district = extract_location(title)

        listings.append(
            RawListing(
                portal=portal,
                external_id=ext_id,
                url=url,
                title=title,
                price=price,
                area_m2=area,
                rooms=rooms,
                bathrooms=parse_baths(block),
                floor=parse_floor(block),
                description=block[:600],
                city=city or "desconocido",
                district=district,
                published_ago_hours=0,     # an alert email IS the new-listing signal
            )
        )

    logger.info(f"Email ingest: {portal} → {len(listings)} listings from {subject[:60]!r}")
    return listings
