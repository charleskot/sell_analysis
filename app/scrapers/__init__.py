from __future__ import annotations

from typing import Dict, Type

from app.scrapers.base import BaseScraper
from app.scrapers.boe import BoeScraper
from app.scrapers.addmeet import AddmeetScraper
from app.scrapers.idealista import IdealistaScraper
from app.scrapers.solvia import SolviaScraper
from app.scrapers.haya import HayaScraper
from app.scrapers.servihabitat import ServihabitatScraper
from app.scrapers.aliseda import AlisedaScraper


SCRAPERS: Dict[str, Type[BaseScraper]] = {
    "boe": BoeScraper,
    "addmeet": AddmeetScraper,
    "idealista": IdealistaScraper,
    "solvia": SolviaScraper,
    "haya": HayaScraper,
    "servihabitat": ServihabitatScraper,
    "aliseda": AlisedaScraper,
}


def get_scrapers(names):
    return [SCRAPERS[n]() for n in names if n in SCRAPERS]
