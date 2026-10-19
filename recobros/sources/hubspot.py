"""Conector HubSpot (CRM API v3) — listo para enchufar.

Config por variables de entorno (mismas claves que el proyecto bihsales):
    HUBSPOT_TOKEN            token privado de la app de HubSpot
    HUBSPOT_PROP_MOROSIDAD   nombre interno de la propiedad de estado (def: estado_recobro)

Dos usos:
  1. `importar_contactos()`  -> enriquece el panel con tel/email desde HubSpot (por email).
  2. `push_estado(email, estado)` -> escribe el estado de morosidad en la ficha del contacto.

Sin token, todo funciona en modo dry-run (registra lo que haría, no llama a la API).
"""
import os

import requests

from .base import AlumnoRecord

API = "https://api.hubapi.com"
PROP_MOROSIDAD = os.getenv("HUBSPOT_PROP_MOROSIDAD", "estado_recobro")


class HubSpotClient:
    def __init__(self, token: str | None = None, prop_morosidad: str | None = None,
                 dry_run: bool | None = None):
        self.token = token or os.getenv("HUBSPOT_TOKEN")
        self.prop = prop_morosidad or PROP_MOROSIDAD
        # dry-run automático si no hay token
        self.dry_run = (self.token is None) if dry_run is None else dry_run

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    # ── Lectura: contactos -> enriquecimiento de contacto ──────────────────
    def importar_contactos(self, limite: int = 1000) -> list[AlumnoRecord]:
        if self.dry_run:
            return []
        records, after = [], None
        props = ["email", "phone", "firstname", "lastname"]
        while len(records) < limite:
            payload = {"limit": 100, "properties": props}
            if after:
                payload["after"] = after
            resp = requests.post(f"{API}/crm/v3/objects/contacts/search",
                                 headers=self._headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for c in data.get("results", []):
                p = c.get("properties", {})
                nombre = " ".join(x for x in [p.get("firstname"), p.get("lastname")] if x)
                records.append(AlumnoRecord(
                    nombre=nombre or (p.get("email") or "").split("@")[0],
                    fecha_matricula=None,   # no aplica: solo contacto
                    tipo_pago="", precio=0,
                    email=p.get("email"), telefono=p.get("phone"),
                    origen="hubspot", ref_externa=c.get("id"),
                    solo_contacto=True))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return records

    # ── Escritura: estado de morosidad en la ficha del contacto ────────────
    def _buscar_contacto_id(self, email: str) -> str | None:
        resp = requests.post(
            f"{API}/crm/v3/objects/contacts/search", headers=self._headers,
            json={"filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}]}],
                "properties": ["email"], "limit": 1}, timeout=30)
        resp.raise_for_status()
        res = resp.json().get("results", [])
        return res[0]["id"] if res else None

    def push_estado(self, email: str, estado_valor: str) -> str:
        """Escribe el estado de morosidad. Devuelve 'dry-run' | 'ok' | 'sin-contacto'."""
        if not email:
            return "sin-contacto"
        if self.dry_run:
            return "dry-run"
        cid = self._buscar_contacto_id(email)
        if not cid:
            return "sin-contacto"
        resp = requests.patch(
            f"{API}/crm/v3/objects/contacts/{cid}", headers=self._headers,
            json={"properties": {self.prop: estado_valor}}, timeout=30)
        resp.raise_for_status()
        return "ok"
