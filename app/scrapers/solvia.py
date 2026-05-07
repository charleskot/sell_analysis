from app.scrapers._bank_base import BankPortalScraper


class SolviaScraper(BankPortalScraper):
    name = "solvia"
    list_url_template = "https://www.solvia.es/es/buscador?provincia={province}&tipoOperacion=venta&tipoInmueble=vivienda"
