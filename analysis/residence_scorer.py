"""Residence match scorer: evaluates listings against personal-home criteria.

Config-driven: any number of profiles under `residence_criteria`. Each profile
has its own hard filters (price, rooms, condition). A listing that matches ANY
profile passes; the winning profile name is stored in the breakdown.

Common requirements (elevator, balcony, min area, min floor, districts) apply
to all profiles.
"""
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class MatchBreakdown:
    matches_profile: str | None = None      # profile name that matched (e.g. "general")
    hard_filters_pass: bool = False
    price_score: float = 0.0
    location_score: float = 0.0
    features_score: float = 0.0
    size_score: float = 0.0
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


def parse_floor(raw: str | None) -> int | None:
    """Parse a floor string into an integer (0 = ground floor, 0.5 not used).

    Handles: '3ª', '3', 'Planta 3', 'Bajo', 'Entresuelo', 'Principal', 'Ático', etc.
    Returns None if unparseable.
    """
    if not raw:
        return None
    s = str(raw).lower().strip()

    if not s or s in ("null", "none", "-"):
        return None

    # Words → floor number
    word_map = {
        "bajo": 0, "planta baja": 0, "pb": 0,
        "entresuelo": 1, "entlo": 1,
        "principal": 2, "pral": 2,
        "ático": 10, "atico": 10, "áticos": 10,
        "sobreático": 11, "sobreatico": 11,
    }
    for word, val in word_map.items():
        if word in s:
            return val

    # Numeric extraction: "3ª", "3", "3ra", "planta 3", etc.
    m = re.search(r"(\d+)", s)
    if m:
        try:
            n = int(m.group(1))
            return n if 0 <= n <= 30 else None
        except ValueError:
            pass
    return None


# ── Main scorer ──────────────────────────────────────────────────────────────

class ResidenceScorer:
    def __init__(self, config: dict):
        self._targets = config.get("target_locations", [])
        self._criteria = config.get("residence_criteria", {})
        self._common = config.get("common_requirements", {})
        self._city_index = {t["city"].lower(): t for t in self._targets}

    def _match_target_location(self, city: str, district: str | None) -> tuple[dict | None, str]:
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
                return target, "not_preferred"

        # No district info — if there's a preferred list, we can't verify
        if preferred:
            return target, "not_preferred"
        return target, "ok"

    def score(self, listing: dict) -> MatchBreakdown:
        bd = MatchBreakdown()

        # ── Hard filter: rented / occupied / squatted ────────────────────────
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
        floor_raw = listing.get("floor")
        condition = (listing.get("condition") or "desconocido").lower()
        city = listing.get("city") or ""
        district = listing.get("district")

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
            bd.reasons_fail.append(f"Barrio no preferido ({city}): {district}")
            return bd

        # ── Hard filter: price/area/rooms data present ───────────────────────
        if not price or not area or not rooms:
            bd.reasons_fail.append("Datos incompletos (precio/m²/hab)")
            return bd

        # ── Hard filter: min area ────────────────────────────────────────────
        min_area = self._common.get("min_area_m2", 60)
        if area < min_area:
            bd.reasons_fail.append(f"Superficie {area:.0f}m² < mínimo {min_area}m²")
            return bd

        # ── Hard filter: min floor ───────────────────────────────────────────
        min_floor = self._common.get("min_floor")
        floor_num = parse_floor(floor_raw)
        if min_floor is not None:
            if floor_num is None:
                # Unknown floor + strict requirement → reject
                bd.reasons_fail.append(f"Planta no declarada (mínimo {min_floor}ª)")
                return bd
            if floor_num < min_floor:
                bd.reasons_fail.append(f"Planta {floor_num} < mínimo {min_floor}")
                return bd

        # ── Hard filter: forbid_condition (across ALL profiles) ──────────────
        # (a listing whose condition is forbidden fails regardless of profile)
        forbid_global = set()
        for prof in self._criteria.values():
            for c in prof.get("forbid_condition") or []:
                forbid_global.add(c.lower())
        if condition in forbid_global:
            bd.reasons_fail.append(f"Estado no aceptado: {condition}")
            return bd

        # ── Profile matching: try each defined profile ───────────────────────
        matched_profile_name = None
        matched_profile = None
        for name, prof in self._criteria.items():
            if self._matches_profile(price, rooms, condition, year_built, prof):
                matched_profile_name = name.replace("profile_", "")
                matched_profile = prof
                break

        if not matched_profile:
            reasons = []
            for name, prof in self._criteria.items():
                soft_max = prof.get("max_price_soft") or prof.get("max_price")
                if soft_max and price > soft_max:
                    reasons.append(f"{name}: precio {price:,.0f}€ > {soft_max:,.0f}€")
                if prof.get("min_rooms") and rooms < prof["min_rooms"]:
                    reasons.append(f"{name}: {rooms} hab < {prof['min_rooms']}")
                required = prof.get("require_condition")
                if required and condition not in required:
                    reasons.append(f"{name}: condición {condition} no en {required}")
            bd.reasons_fail.append(" | ".join(reasons[:3]) or "No cumple ningún perfil")
            return bd

        bd.matches_profile = matched_profile_name
        bd.reasons_pass.append(matched_profile.get("label", matched_profile_name))

        # ── Hard filter: elevator ────────────────────────────────────────────
        if self._common.get("require_elevator") and has_elevator is False:
            bd.reasons_fail.append("Sin ascensor (obligatorio)")
            return bd

        # ── Hard filter: balcony (only if required) ──────────────────────────
        # If the listing description doesn't mention balcony BUT require_balcony=True,
        # we do soft-warn (many listings just don't mention it, don't want to lose them)

        # ── Soft score (0-100) ───────────────────────────────────────────────
        bd.hard_filters_pass = True

        max_p = matched_profile.get("max_price_soft") or matched_profile.get("max_price", 400_000)
        ratio = price / max_p
        bd.price_score = max(30.0, min(100.0, 100.0 - (ratio - 0.6) * 175.0))

        bd.location_score = 100.0 if loc_status == "preferred" else 70.0

        feat = 0
        if has_elevator is True:
            feat += 40
        elif has_elevator is None:
            feat += 20
        if has_balcony is True:
            feat += 40
        elif has_balcony is None:
            feat += 15
        if has_views is True:
            feat += 20
        bd.features_score = float(min(100, feat))
        if has_balcony is None and self._common.get("require_balcony"):
            bd.reasons_pass.append("⚠ balcón no confirmado")

        rooms_norm = min(rooms, 4) / 4
        area_norm = min(area, 120) / 120
        bd.size_score = round((rooms_norm * 0.4 + area_norm * 0.6) * 100, 1)

        cond_map = {"nuevo": 100, "buen_estado": 80, "desconocido": 60, "reformar": 30}
        bd.condition_score = float(cond_map.get(condition, 60))

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

        forbidden = profile.get("forbid_condition") or []
        if condition in [c.lower() for c in forbidden]:
            return False

        max_years = profile.get("max_years_old")
        if max_years and year_built:
            age = datetime.now().year - year_built
            if age > max_years:
                return False

        return True
