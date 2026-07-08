"""Residence match scorer: evaluates listings against personal-home criteria.

Unlike InvestmentScorer (which optimises yield), this scores how well a listing
matches the user's stated preferences for a primary residence.

Two profiles evaluated:
  A) New-build: max_price 360k, ≥2 rooms
  B) Recent (≤20 years): max_price 285k (soft 315k), ≥3 rooms

Common requirements: elevator, balcony, ≥60m², preferred/allowed districts.
"""
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class MatchBreakdown:
    matches_profile: str | None = None      # "new" | "recent" | None
    hard_filters_pass: bool = False
    price_score: float = 0.0                # closer to budget → higher
    location_score: float = 0.0             # preferred district bonus
    features_score: float = 0.0             # elevator, balcony, views
    size_score: float = 0.0                 # more rooms/m² → higher
    condition_score: float = 0.0
    total: float = 0.0
    reasons_pass: list[str] = None
    reasons_fail: list[str] = None

    def __post_init__(self):
        if self.reasons_pass is None:
            self.reasons_pass = []
        if self.reasons_fail is None:
            self.reasons_fail = []

    def to_dict(self) -> dict:
        return asdict(self)


# ── Feature extraction from title + description ──────────────────────────────

_ELEVATOR_RE = re.compile(r"\bascensor(?:es)?\b|\belevador\b", re.I)
_NO_ELEVATOR_RE = re.compile(r"\bsin\s+ascensor\b", re.I)
_BALCONY_RE = re.compile(r"\bbalc[oó]n(?:es)?\b|\bterraza\b|\bterrat\b|\bpatio\b", re.I)
_VIEWS_RE = re.compile(r"\bvistas?\b|\bpanor[aá]mic\w*\b|\bmar\b|\bmonta[nñ]a\b", re.I)
_YEAR_RE = re.compile(r"\bconstru[íi]do\s+(?:en\s+)?(\d{4})\b|\ba[nñ]o\s+(?:de\s+construcci[oó]n[:\s]+)?(\d{4})\b", re.I)

# Rented or occupied → discard (buying a home to live in, not investment)
_RENTED_RE = re.compile(
    r"\balquilad[oa]\b|\bcon\s+inquilino\b|\barrendad[oa]\b|"
    r"\brenta\s+antigua\b|\bcontrato\s+de\s+alquiler\b|"
    r"\balquiler\s+vigente\b|\brentabilidad\s+garantizada\b",
    re.I,
)
_OCCUPIED_RE = re.compile(
    r"\bocupad[oa]\b|\bokupad[oa]\b|\bokupas?\b|"
    r"\busurpaci[oó]n\b|\busurpad[oa]\b|"
    r"\bcon\s+ocupantes?\b|\bilegalmente\s+ocupad[oa]\b",
    re.I,
)


def extract_features(listing: dict) -> dict:
    """Parse text fields to detect elevator, balcony, views, year built."""
    text = " ".join(str(listing.get(k) or "") for k in ("title", "description"))

    has_elevator = None
    if _NO_ELEVATOR_RE.search(text):
        has_elevator = False
    elif _ELEVATOR_RE.search(text):
        has_elevator = True

    has_balcony = True if _BALCONY_RE.search(text) else None
    has_views = True if _VIEWS_RE.search(text) else None

    year_built = None
    m = _YEAR_RE.search(text)
    if m:
        try:
            year_built = int(m.group(1) or m.group(2))
        except (TypeError, ValueError):
            pass

    return {
        "has_elevator": has_elevator,
        "has_balcony": has_balcony,
        "has_views": has_views,
        "year_built": year_built,
    }


# ── Main scorer ──────────────────────────────────────────────────────────────

class ResidenceScorer:
    def __init__(self, config: dict):
        self._targets = config.get("target_locations", [])
        self._criteria = config.get("residence_criteria", {})
        self._common = config.get("common_requirements", {})
        self._city_index = {t["city"].lower(): t for t in self._targets}

    def _match_target_location(self, city: str, district: str | None) -> tuple[dict | None, str]:
        """Return (target_entry, status). status: 'preferred' | 'ok' | 'excluded' | 'not_targeted'."""
        city_key = (city or "").lower().strip()
        district_key = (district or "").lower().strip()

        target = None
        for tcity, tentry in self._city_index.items():
            if tcity == city_key or tcity in city_key or city_key in tcity:
                target = tentry
                break

        if not target:
            return None, "not_targeted"

        preferred = [d.lower() for d in target.get("preferred_districts", [])]
        excluded = [d.lower() for d in target.get("excluded_districts", [])]

        if district_key:
            if any(exc in district_key for exc in excluded):
                return target, "excluded"
            if preferred:
                if any(pref in district_key for pref in preferred):
                    return target, "preferred"
                # If preferred list exists but no match → not in accepted area
                return target, "not_preferred"

        return target, "ok"

    def score(self, listing: dict) -> MatchBreakdown:
        bd = MatchBreakdown()

        # ── Hard filter: rented / occupied / squatted ────────────────────────
        # Buying to live in — no tenants, no okupas
        text = " ".join(str(listing.get(k) or "") for k in ("title", "description"))
        if _RENTED_RE.search(text):
            bd.reasons_fail.append("Alquilado / con inquilino")
            return bd
        if _OCCUPIED_RE.search(text):
            bd.reasons_fail.append("Ocupado / okupado")
            return bd

        price = listing.get("price")
        area = listing.get("area_m2")
        rooms = listing.get("rooms")
        condition = (listing.get("condition") or "desconocido").lower()
        city = listing.get("city") or ""
        district = listing.get("district")

        # Feature extraction (elevator, balcony, views, year)
        features = extract_features(listing)
        has_elevator = features["has_elevator"]
        has_balcony = features["has_balcony"]
        has_views = features["has_views"]
        year_built = features["year_built"]

        # ── Hard filter: location ────────────────────────────────────────────
        target, loc_status = self._match_target_location(city, district)
        if loc_status == "not_targeted":
            bd.reasons_fail.append(f"Ciudad fuera de zona objetivo: {city}")
            return bd
        if loc_status == "excluded":
            bd.reasons_fail.append(f"Barrio excluido: {district}")
            return bd
        if loc_status == "not_preferred":
            bd.reasons_fail.append(f"Barrio no está en la zona preferida ({city}): {district}")
            return bd

        # ── Hard filter: price/area/rooms sanity ─────────────────────────────
        if not price or not area or not rooms:
            bd.reasons_fail.append("Datos incompletos (precio/m²/hab)")
            return bd

        min_area = self._common.get("min_area_m2", 60)
        if area < min_area:
            bd.reasons_fail.append(f"Superficie {area:.0f}m² < mínimo {min_area}m²")
            return bd

        # ── Profile matching ─────────────────────────────────────────────────
        profile_new = self._criteria.get("profile_new", {})
        profile_recent = self._criteria.get("profile_recent", {})

        matches_new = self._matches_profile(price, rooms, condition, year_built, profile_new)
        matches_recent = self._matches_profile(price, rooms, condition, year_built, profile_recent)

        if not matches_new and not matches_recent:
            reasons = []
            if condition == "nuevo":
                if price > profile_new.get("max_price", 999_999_999):
                    reasons.append(f"Obra nueva {price:,.0f}€ > 360k€")
            if year_built and datetime.now().year - year_built > 20:
                reasons.append(f"Antigüedad {datetime.now().year - year_built}a > 20a")
            if rooms < 2:
                reasons.append(f"{rooms} hab. < mínimo 2")
            bd.reasons_fail.append(" | ".join(reasons) or "No cumple ninguno de los 2 perfiles")
            return bd

        if matches_new:
            bd.matches_profile = "new"
            bd.reasons_pass.append("Obra nueva ≤360k, ≥2 hab")
        elif matches_recent:
            bd.matches_profile = "recent"
            soft = profile_recent.get("max_price_soft", 315_000)
            if price > profile_recent.get("max_price", 285_000):
                bd.reasons_pass.append(f"Reciente ≤20a, ≥3 hab (precio {price:,.0f}€ negociable)")
            else:
                bd.reasons_pass.append("Reciente ≤20a, ≥3 hab, ≤285k")

        # ── Hard filter: elevator / balcony ──────────────────────────────────
        # If required and explicitly denied → fail. If unknown → soft-warn.
        if self._common.get("require_elevator") and has_elevator is False:
            bd.reasons_fail.append("Sin ascensor (obligatorio)")
            return bd

        # ── Now compute soft score (0-100) ───────────────────────────────────
        bd.hard_filters_pass = True

        # Price score: better if further under budget
        if bd.matches_profile == "new":
            max_p = profile_new.get("max_price", 360_000)
        else:
            max_p = profile_recent.get("max_price_soft", 315_000)
        ratio = price / max_p
        # 100 at 60% of budget, linearly down to 30 at 100%
        bd.price_score = max(30.0, min(100.0, 100.0 - (ratio - 0.6) * 175.0))

        # Location score: preferred district > ok
        bd.location_score = 100.0 if loc_status == "preferred" else 70.0

        # Features score: elevator, balcony, views
        feat = 0
        if has_elevator is True:
            feat += 40
        elif has_elevator is None:
            feat += 20  # unknown → half credit
        if has_balcony is True:
            feat += 40
        elif has_balcony is None:
            feat += 15
        if has_views is True:
            feat += 20
        bd.features_score = float(min(100, feat))
        if has_balcony is None and self._common.get("require_balcony"):
            bd.reasons_pass.append("⚠ balcón no confirmado")

        # Size score
        rooms_norm = min(rooms, 4) / 4  # 4+ rooms → 100
        area_norm = min(area, 120) / 120
        bd.size_score = round((rooms_norm * 0.4 + area_norm * 0.6) * 100, 1)

        # Condition score
        cond_map = {"nuevo": 100, "buen_estado": 80, "desconocido": 60, "reformar": 30}
        bd.condition_score = float(cond_map.get(condition, 60))

        # Total weighted
        bd.total = round(
            bd.price_score * 0.30
            + bd.location_score * 0.25
            + bd.features_score * 0.25
            + bd.size_score * 0.15
            + bd.condition_score * 0.05,
            1,
        )

        return bd

    def _matches_profile(self, price: float, rooms: int, condition: str,
                          year_built: int | None, profile: dict) -> bool:
        max_price = profile.get("max_price")
        soft_max = profile.get("max_price_soft") or max_price
        if soft_max and price > soft_max:
            return False

        if profile.get("min_rooms") and rooms < profile["min_rooms"]:
            return False

        required = profile.get("require_condition")
        if required and condition not in required:
            return False

        max_years = profile.get("max_years_old")
        if max_years and year_built:
            age = datetime.now().year - year_built
            if age > max_years:
                return False

        return True
