from app.scrapers._bank_base import BankPortalScraper


class HayaScraper(BankPortalScraper):
    name = "haya"
    list_url_template = "https://www.haya.es/inmuebles?provincia={province}&tipo=vivienda"
