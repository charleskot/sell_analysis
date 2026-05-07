from app.scrapers._bank_base import BankPortalScraper


class ServihabitatScraper(BankPortalScraper):
    name = "servihabitat"
    list_url_template = "https://www.servihabitat.com/es/buscador?provincia={province}&tipo=vivienda"
