"""Rent estimator: DB lookup → fallback table."""
import logging
import re

logger = logging.getLogger(__name__)


class RentEstimator:
    def __init__(self, config: dict):
        self._fallback: dict[str, float] = config.get("fallback_rent_per_m2", {})
        self._default = self._fallback.get("default", 5.5)

    # Rentable area cap: large properties don't scale linearly beyond ~100m²
    # (spare rooms, storage, etc. don't proportionally increase rent)
    _MAX_RENTABLE_M2 = 120.0
    _LARGE_AREA_DISCOUNT = 0.70  # >120m² extra area counts at 70% efficiency

    def estimate_monthly_rent(self, city: str, district: str | None, area_m2: float) -> float:
        """Returns estimated monthly rent based on zone averages."""
        rent_per_m2 = self._get_rent_per_m2(city, district)
        # Apply diminishing returns for large properties
        if area_m2 > self._MAX_RENTABLE_M2:
            effective_area = self._MAX_RENTABLE_M2 + (area_m2 - self._MAX_RENTABLE_M2) * self._LARGE_AREA_DISCOUNT
        else:
            effective_area = area_m2
        return round(rent_per_m2 * effective_area, 2)

    def _get_rent_per_m2(self, city: str, district: str | None) -> float:
        """Lookup rent per m²: DB first, then fallback table."""
        # Try DB lookup
        db_val = self._db_lookup(city, district)
        if db_val:
            return db_val

        # Normalize city name
        city_key = self._normalize_city(city)

        # Direct match in fallback table
        if city_key in self._fallback:
            return self._fallback[city_key]

        # Try province fallback (when city is a town in a known province)
        province = self._get_province(city_key)
        if province and province in self._fallback:
            # Use 80% of province capital rate for suburban/rural towns
            return self._fallback[province] * 0.75

        logger.debug(f"No rent data for {city}/{district}, using default {self._default}")
        return self._default

    def _db_lookup(self, city: str, district: str | None) -> float | None:
        try:
            from models.db import get_rent_zone_average
            return get_rent_zone_average(city, district)
        except Exception:
            return None

    def _normalize_city(self, city: str) -> str:
        city = city.lower().strip()

        # Servihabitat format: "distrito <name>. <city> capital"
        m = re.match(r"distrito\s+.+\.\s+(.+?)(?:\s+capital)?$", city)
        if m:
            city = m.group(1).strip()

        # Strip trailing " capital" / " - capital"
        city = re.sub(r"\s*[-–]?\s*capital$", "", city).strip()

        # Canonical name map (spelling variants)
        canonical = {
            "madrid": "madrid",
            "barcelona": "barcelona",
            "barcelona-capital": "barcelona",
            "valència": "valencia",
            "valencia": "valencia",
            "sevilla": "sevilla",
            "seville": "sevilla",
            "zaragoza": "zaragoza",
            "málaga": "malaga",
            "malaga": "malaga",
            "bilbao": "bilbao",
            "bilbo": "bilbao",
            "palma": "palma",
            "palma de mallorca": "palma",
            "alicante": "alicante",
            "alacant": "alicante",
            "granada": "granada",
            # accent variants for suburbs
            "alcorcón": "alcorcón",
            "móstoles": "móstoles",
            "alcobendas": "alcobendas",
            "fuenlabrada": "fuenlabrada",
            "leganés": "leganés",
            "rivas-vaciamadrid": "rivas-vaciamadrid",
            "pozuelo de alarcón": "pozuelo de alarcón",
            "las rozas de madrid": "las rozas de madrid",
            "collado villalba": "collado villalba",
            "san sebastián de los reyes": "san sebastián de los reyes",
            "majadahonda": "majadahonda",
            "alcalá de henares": "alcalá de henares",
            "torrejón de ardoz": "torrejón de ardoz",
            "hospitalet de llobregat": "hospitalet de llobregat",
            "l'hospitalet de llobregat": "hospitalet de llobregat",
        }
        return canonical.get(city, city)

    def _get_province(self, city: str) -> str | None:
        """Return province capital for a known suburb/town."""
        madrid_towns = {
            "aranjuez", "becerril de la sierra", "escorial (el)", "el escorial",
            "navalcarnero", "arganda del rey", "valdemoro", "pinto", "mejorada del campo",
            "torrejón de la calzada", "ciempozuelos", "san fernando de henares",
            "boadilla del monte", "alcobendas", "getafe", "alcorcón", "leganés",
            "fuenlabrada", "móstoles", "alcalá de henares", "torrejón de ardoz",
            "parla", "san sebastián de los reyes", "pozuelo de alarcón", "majadahonda",
            "las rozas de madrid", "rivas-vaciamadrid", "coslada", "tres cantos",
            "collado villalba", "valdemoro", "arganda del rey",
        }
        barcelona_towns = {
            "hospitalet de llobregat", "l'hospitalet de llobregat", "badalona",
            "sabadell", "terrassa", "mataró", "cornellà de llobregat", "sant boi de llobregat",
            "rubí", "viladecans", "castelldefels", "gavà", "mollet del vallès", "granollers",
            "manresa", "igualada", "santa coloma de gramenet",
        }
        valencia_towns = {
            "torrent", "paterna", "burjassot", "mislata", "alzira", "gandia",
            "l'alcúdia", "sueca", "ontinyent", "benidorm", "manises",
        }
        seville_towns = {
            "dos hermanas", "alcalá de guadaíra", "mairena del aljarafe",
            "utrera", "la rinconada", "camas", "san juan de aznalfarache",
        }
        malaga_towns = {
            "torremolinos", "benalmádena", "fuengirola", "mijas", "estepona",
            "marbella", "nerja", "vélez-málaga", "alhaurín el grande",
        }

        if city in madrid_towns:
            return "madrid"
        if city in barcelona_towns:
            return "barcelona"
        if city in valencia_towns:
            return "valencia"
        if city in seville_towns:
            return "sevilla"
        if city in malaga_towns:
            return "malaga"
        return None
