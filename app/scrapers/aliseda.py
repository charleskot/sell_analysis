from app.scrapers._bank_base import BankPortalScraper


class AlisedaScraper(BankPortalScraper):
    name = "aliseda"
    list_url_template = "https://www.alisedainmobiliaria.com/es/buscar-inmuebles?provincia={province}&tipo=vivienda"
