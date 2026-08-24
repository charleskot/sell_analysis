"""Search profiles: several searches running at once over the same feed.

Buying a home to live in and buying one to rent out are different questions
with contradictory criteria — one wants a specific set of streets and a flat
that needs no work, the other wants a number. A single filter cannot serve
both without making each worse.

Every listing is evaluated against every enabled profile. Matching any one is
enough to alert, and the alert names the profile so the user knows which of
their searches just fired.
"""
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


def normalise(text: str | None) -> str:
    """Lowercase, strip accents and collapse punctuation.

    Place names arrive spelled every possible way: "Sagrada Família",
    "sagrada familia", "La Sagrera", "l'Hospitalet de Llobregat". Comparing
    them raw produces silent misses, which here means never being told about
    a whole neighbourhood.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFD", str(text).lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[''`´]", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_ARTICLES_RE = re.compile(r"\b(?:el|la|els|les|los|las|lo|l)\b")


def _strip_articles(text: str) -> str:
    return re.sub(r"\s+", " ", _ARTICLES_RE.sub(" ", text)).strip()


def _place_matches(needle: str, haystack: str) -> bool:
    """Substring match on normalised place names, either direction.

    Config says "hospitalet", the portal says "hospitalet de llobregat";
    config says "sant andreu", the portal says "sant andreu de palomar".

    Articles are dropped on a second pass because the two sides rarely
    agree on them: config says "el guinardo", Habitaclia truncates the
    district to "Guinardó (Horta G" — no substring in either direction, and
    a 164.000 € flat squarely inside the wanted zone died on this check.
    The same miss let "Lloreda (Salut" in Badalona slip past the excluded
    zone written as "la salut".
    """
    n, h = normalise(needle), normalise(haystack)
    if not n or not h:
        return False
    if n in h or h in n:
        return True
    ns, hs = _strip_articles(n), _strip_articles(h)
    return bool(ns and hs and (ns in hs or hs in ns))


class Profile:
    """One saved search with its own criteria.

    `purpose` separates two opposite readings of the same numbers:

      home        the buyer lives there. There is no rent, so yield and
                  cashflow are meaningless, and stretching — a second loan
                  for the entry — is an accepted cost of getting the flat.

      investment  the flat has to fund itself. It must be reachable with
                  the cash in hand, and pay its own way from month one.
                  Borrowing to cover the entry defeats the point.
    """

    def __init__(self, spec: dict):
        self.name = spec.get("name", "sin_nombre")
        self.label = spec.get("label", self.name)
        self.enabled = spec.get("enabled", True)
        self.purpose = spec.get("purpose", "investment")
        self.spec = spec

    # ── Individual conditions ────────────────────────────────────────────

    def _check_price(self, price: float) -> str | None:
        max_price = self.spec.get("max_price")
        if max_price and price > max_price:
            return f"precio {price:,.0f}€ > {max_price:,.0f}€"
        min_price = self.spec.get("min_price")
        if min_price and price < min_price:
            return f"precio {price:,.0f}€ < {min_price:,.0f}€"
        return None

    def _check_rooms(self, rooms: int | None) -> str | None:
        # Unknown room count is a parsing gap, not a reason to discard
        if rooms is None:
            return None
        min_rooms = self.spec.get("min_rooms")
        if min_rooms and rooms < min_rooms:
            return f"{rooms} hab < {min_rooms}"
        max_rooms = self.spec.get("max_rooms")
        if max_rooms and rooms > max_rooms:
            return f"{rooms} hab > {max_rooms}"
        return None

    def _check_size(self, area: float | None) -> str | None:
        min_area = self.spec.get("min_area_m2")
        if not min_area or area is None:
            return None
        if area < min_area:
            return f"{area:.0f}m² < mínimo {min_area}m²"
        return None

    def _check_floor(self, listing: dict) -> str | None:
        """Reject ground floors and the like.

        A bajo is darker, noisier and easier to break into, and resells worse
        — which matters as much for a home as the price does.
        """
        min_floor = self.spec.get("min_floor")
        if min_floor is None:
            return None

        from analysis.residence_scorer import parse_floor

        floor = parse_floor(listing.get("floor"))
        if floor is None:
            # Fall back to the description: portals often name the floor there
            # even when the structured field is empty.
            text = " ".join(str(listing.get(k) or "") for k in ("title", "description"))
            if re.search(r"\bbajos?\b|\bplanta\s+baja\b|\bentresuelo\b|\bsemis[oó]tano\b",
                         text, re.I):
                return "es un bajo o entresuelo"
            if self.spec.get("reject_unknown_floor", False):
                return "planta no declarada"
            return None

        if floor < min_floor:
            return f"planta {floor} < mínimo {min_floor}"
        return None

    def _check_services(self, city: str) -> str | None:
        """Reject towns too small to have everyday services.

        Size stands in for supermarket, health centre, school and train —
        the things that decide whether a flat lets quickly and sells again
        without a year on the market. A village of 2.000 people may be
        cheap, but the exit is the problem, not the entry.
        """
        min_pop = self.spec.get("min_population")
        if not min_pop:
            return None

        from analysis.municipalities import population_of

        pop = population_of(city)
        if pop is None:
            # Unknown is not the same as small. Whether that is acceptable is
            # the profile's call: for an investment it usually is not.
            if self.spec.get("reject_unknown_population", False):
                return f"población de '{city}' desconocida"
            return None

        if pop < min_pop:
            return f"{city} tiene ~{pop:,} hab. (mínimo {min_pop:,})"
        return None

    def _check_location(self, city: str, district: str | None) -> str | None:
        """Match against areas, each tying a set of districts to its town.

        Districts must be scoped to a city or street names produce false
        matches: "Passeig Maragall" is a street in Gavà as well as a
        neighbourhood in Barcelona, and an unscoped match files the Gavà flat
        under a Barcelona search.
        """
        excluded = self.spec.get("excluded_cities") or []
        if excluded and any(_place_matches(x, city) for x in excluded):
            return f"ciudad '{city}' excluida"

        # Zones the buyer has ruled out. Matched against district and city
        # together, so either level can be named.
        excluded_zones = self.spec.get("excluded_zones") or []
        if excluded_zones:
            where = f"{district or ''} {city or ''}"
            if any(_place_matches(z, where) for z in excluded_zones):
                return f"zona excluida: {district or city}"

        areas = self.spec.get("areas") or []
        if not areas:
            return None

        matched_city = False
        for area in areas:
            area_city = area.get("city", "")
            if not _place_matches(area_city, city):
                continue
            matched_city = True

            districts = area.get("districts") or []
            if not districts:
                return None       # whole town accepted

            # District names sometimes arrive inside the city field, and are
            # often truncated by the portal, so match against both.
            where = f"{district or ''} {city or ''}"
            if any(_place_matches(d, where) for d in districts):
                return None

        if matched_city:
            return f"barrio '{district or '?'}' fuera de zona en {city}"
        return f"ciudad '{city}' fuera de zona"

    def _check_condition(self, listing: dict, price: float) -> str | None:
        """Move-in-ready requirement, with a discount escape hatch.

        Wanting a flat that needs no work is a preference, not an absolute:
        below a certain price a refurbishment becomes part of the deal rather
        than an inconvenience. `needs_work_below_price` is where that flips.
        """
        required = self.spec.get("condition_in") or []
        if not required:
            return None

        text = " ".join(
            str(listing.get(k) or "") for k in ("title", "description", "condition")
        )
        needs_work = re.search(r"\bpara\s+reformar\b|\ba\s+reformar\b|\breformar\b", text, re.I)

        if not needs_work:
            return None

        cheap_enough = self.spec.get("needs_work_below_price")
        if cheap_enough and price <= cheap_enough:
            return None
        return "necesita reforma"

    def _check_features(self, listing: dict) -> str | None:
        """Require at least one of a set of words to appear in the advert.

        Used for things a listing either mentions or does not — a balcony, a
        terrace. Absence is treated as absence: adverts that have one say so,
        because it sells the flat.
        """
        required = self.spec.get("require_any_of") or []
        if not required:
            return None

        text = normalise(
            " ".join(str(listing.get(k) or "") for k in ("title", "description"))
        )
        if any(normalise(term) in text for term in required):
            return None
        return f"no menciona {' ni '.join(required[:3])}"

    def _check_verified(self, metrics: dict) -> str | None:
        """Require the listing's own page to have been read and cleared.

        Alert emails omit the condition for most listings, and the cheapest
        flats are cheap precisely because they are occupied or unmortgageable.
        Silence is therefore not neutral: unverified plus suspiciously cheap
        is the shape of exactly the listing that must not be sent.
        """
        if not self.spec.get("require_verified_condition"):
            return None
        if metrics.get("condition_unknown"):
            return "estado sin verificar (no he podido leer el anuncio)"
        return None

    def _check_financials(self, metrics: dict) -> tuple[bool, str | None]:
        """Returns (has_financial_criteria, failure_reason)."""
        min_yield = self.spec.get("min_net_yield_pct")
        min_cashflow = self.spec.get("min_monthly_cashflow")
        max_cash = self.spec.get("max_cash_needed")
        max_gap = self.spec.get("max_cash_gap")

        if all(v is None for v in (min_yield, min_cashflow, max_cash, max_gap)):
            return False, None

        # Upfront cash is a hard constraint: no yield makes an unaffordable
        # deal affordable.
        if max_cash is not None:
            cash_needed = metrics.get("cash_needed")
            if cash_needed is not None and cash_needed > max_cash:
                return True, f"necesita {cash_needed:,.0f}€ > {max_cash:,.0f}€"

        # An investment that needs a loan just to cover its own entry is not
        # funding itself, however good the yield looks.
        if max_gap is not None:
            gap = metrics.get("cash_gap")
            if gap is not None and gap > max_gap:
                return True, f"habría que pedir {gap:,.0f}€ prestados para la entrada"

        net_yield = metrics.get("net_yield_pct") or 0
        cashflow = metrics.get("monthly_cashflow")

        # Both bars must clear when both are set: a flat that yields well on
        # paper but loses money every month is not paying its own way.
        if min_yield is not None and net_yield < min_yield:
            return True, f"rentabilidad neta {net_yield:.2f}% < {min_yield}%"

        if min_cashflow is not None:
            if cashflow is None:
                return True, "sin cashflow calculable"
            if cashflow <= min_cashflow:
                return True, f"cashflow {cashflow:+,.0f}€/mes"

        return True, None

    # ── Public API ───────────────────────────────────────────────────────

    def match(self, listing: dict, metrics: dict) -> tuple[bool, str]:
        """Evaluate a listing. Returns (matches, human-readable reason)."""
        price = listing.get("price") or 0
        city = listing.get("city") or ""
        district = listing.get("district")

        for failure in (
            self._check_price(price),
            self._check_rooms(listing.get("rooms")),
            self._check_size(listing.get("area_m2")),
            self._check_floor(listing),
            self._check_location(city, district),
            self._check_services(city),
            self._check_condition(listing, price),
            self._check_features(listing),
            self._check_verified(metrics),
        ):
            if failure:
                return False, failure

        had_financials, failure = self._check_financials(metrics)
        if failure:
            return False, failure

        # The reason says which of the requirements this flat clears. It used
        # to restate the entry cash and the monthly payment, both of which the
        # alert already shows in full a few lines above — so it told the
        # reader nothing they could not already see, while the thing they
        # cannot derive, which search this answers and on what grounds, went
        # unsaid.
        criteria = self._criteria_met(listing)

        if self.purpose == "home":
            return True, " · ".join(criteria) or "cumple zona, precio y estado"

        if had_financials:
            net_yield = metrics.get("net_yield_pct") or 0
            cashflow = metrics.get("monthly_cashflow")
            parts = [f"rentabilidad neta {net_yield:.2f}%"]
            if cashflow is not None:
                parts.append(f"cashflow {cashflow:+,.0f}€/mes".replace(",", "."))
            return True, " · ".join(parts + criteria)

        return True, " · ".join(criteria) or "cumple criterios de zona, precio y estado"

    def _criteria_met(self, listing: dict) -> list[str]:
        """The requirements this listing clears, named against the bar set.

        "66m² (pides ≥65)" is worth a line; "66m²" on its own is already in
        the card.
        """
        out = []

        area = listing.get("area_m2")
        min_area = self.spec.get("min_area_m2")
        if area and min_area:
            out.append(f"{area:.0f}m² (pides ≥{min_area:.0f})")

        rooms = listing.get("rooms")
        min_rooms = self.spec.get("min_rooms")
        if rooms is not None and min_rooms:
            out.append(f"{rooms} hab (pides ≥{min_rooms})")

        if self.spec.get("min_floor"):
            out.append("no es bajo")

        required = self.spec.get("require_any_of") or []
        if required:
            haystack = normalise(
                f"{listing.get('title', '')} {listing.get('description', '')}"
            )
            found = next((f for f in required if normalise(f) in haystack), None)
            if found:
                out.append(found)

        where = listing.get("district") or listing.get("city")
        if where and self.spec.get("areas"):
            out.append(f"{where.title()} está en tu lista")

        condition = listing.get("condition")
        if condition and self.spec.get("condition_in"):
            out.append({"nuevo": "obra nueva", "buen_estado": "buen estado",
                        "a_reformar": "a reformar"}.get(condition, condition))

        return out


class ProfileMatcher:
    """Evaluates listings against every enabled profile."""

    def __init__(self, config: dict):
        specs = config.get("search_profiles") or []
        self.profiles = [Profile(s) for s in specs if s.get("enabled", True)]
        if self.profiles:
            names = ", ".join(p.name for p in self.profiles)
            logger.info(f"Perfiles activos: {names}")
        else:
            logger.warning("No hay perfiles de búsqueda activos — no se enviará ninguna alerta")

    def match(self, listing: dict, metrics: dict) -> list[tuple[Profile, str]]:
        """All profiles this listing satisfies, with the reason for each."""
        hits = []
        for profile in self.profiles:
            ok, reason = profile.match(listing, metrics)
            if ok:
                hits.append((profile, reason))
            else:
                logger.debug(f"{listing.get('url', '?')} descartado por {profile.name}: {reason}")
        return hits
