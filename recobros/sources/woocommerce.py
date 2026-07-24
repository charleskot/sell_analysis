"""Conector WooCommerce (REST API v3) — listo para enchufar.

Config por variables de entorno (mismas claves que el proyecto bihsales):
    WOO_URL       https://tienda.com
    WOO_KEY       consumer key
    WOO_SECRET    consumer secret

Lee los pedidos y los convierte en `AlumnoRecord` + pagos. La venta a plazos en
WooCommerce depende del plugin que la gestione (Subscriptions, Deposits, Sequra,
Nemuru...). `_tipo_pago_desde_pedido` centraliza esa heurística: ajústala cuando
confirmemos qué plugin usa la tienda (dato pendiente de bihsales).

Sin claves, opera en dry-run (no llama a la API).
"""
import os
from datetime import date, datetime

import requests

from .base import AlumnoRecord

# métodos de pago (order.payment_method) que implican financiación externa a plazos
METODOS_FINANCIACION = {"sequra", "nemuru", "aplazame", "pagantis", "scalapay"}


class WooClient:
    def __init__(self, url: str | None = None, key: str | None = None,
                 secret: str | None = None, dry_run: bool | None = None):
        self.url = (url or os.getenv("WOO_URL", "")).rstrip("/")
        self.key = key or os.getenv("WOO_KEY")
        self.secret = secret or os.getenv("WOO_SECRET")
        self.dry_run = (not all([self.url, self.key, self.secret])
                        if dry_run is None else dry_run)

    def _get(self, endpoint: str, params: dict) -> list:
        resp = requests.get(f"{self.url}/wp-json/wc/v3/{endpoint}",
                            params=params, auth=(self.key, self.secret), timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _fecha(valor: str) -> date:
        try:
            return datetime.fromisoformat(valor.replace("Z", "")).date()
        except (ValueError, AttributeError):
            return date.today()

    @staticmethod
    def _tipo_pago_desde_pedido(pedido: dict) -> str:
        metodo = (pedido.get("payment_method") or "").lower()
        if any(m in metodo for m in METODOS_FINANCIACION):
            return "Nemuru"        # financiación externa: solo pago inicial en el panel
        # meta que algunos plugins de plazos/depósitos añaden al pedido
        metas = {m.get("key"): m.get("value") for m in pedido.get("meta_data", [])}
        if any(k in metas for k in ("_wc_deposit_type", "_deposit_amount", "_installment_plan")):
            return "Plazos"
        return "Contado"

    def importar_pedidos(self, desde: str | None = None, estado: str = "any",
                         max_paginas: int = 20) -> list[AlumnoRecord]:
        """Convierte pedidos de la tienda en registros de alumno."""
        if self.dry_run:
            return []
        records, pagina = [], 1
        while pagina <= max_paginas:
            params = {"per_page": 100, "page": pagina, "status": estado,
                      "orderby": "date", "order": "asc"}
            if desde:
                params["after"] = desde
            pedidos = self._get("orders", params)
            if not pedidos:
                break
            for o in pedidos:
                bill = o.get("billing", {})
                email = bill.get("email")
                nombre = " ".join(x for x in [bill.get("first_name"),
                                              bill.get("last_name")] if x)
                tipo_pago = self._tipo_pago_desde_pedido(o)
                precio = float(o.get("total") or 0)
                # pago inicial: lo ya pagado (total si el pedido está 'completed')
                pagado = precio if o.get("status") in ("completed", "processing") else 0.0
                records.append(AlumnoRecord(
                    nombre=nombre or (email or "").split("@")[0],
                    fecha_matricula=self._fecha(o.get("date_created")),
                    tipo_pago=tipo_pago,
                    precio=precio,
                    primer_pago=pagado,
                    email=email,
                    telefono=bill.get("phone"),
                    canal="WooCommerce",
                    edicion=(o.get("line_items") or [{}])[0].get("name"),
                    origen="woocommerce",
                    ref_externa=str(o.get("id")),
                ))
            pagina += 1
        return records
