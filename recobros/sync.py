"""Orquestador de sincronización del panel de recobros.

Uso (CLI):
    python -m recobros.sync excel data/matriculas_2023.xlsx   # sube un Excel histórico
    python -m recobros.sync woo                                # importa pedidos de WooCommerce
    python -m recobros.sync hubspot-contactos                  # enriquece contactos desde HubSpot
    python -m recobros.sync push-estados                       # escribe morosidad en HubSpot
    python -m recobros.sync all                                # woo + hubspot-contactos + push-estados

Sin claves configuradas, los pasos de HubSpot/Woo funcionan en dry-run (no llaman
a la API) para poder probar el flujo completo sin credenciales.
"""
import sys
from datetime import date

from .db import get_conn
from .logic import cargar_panel, estado_hubspot
from .sources.base import upsert_alumnos
from .sources.excel_source import leer_archivo
from .sources.hubspot import HubSpotClient
from .sources.woocommerce import WooClient


def sync_excel(path, conn=None) -> dict:
    conn = conn or get_conn()
    records = leer_archivo(path)
    res = upsert_alumnos(conn, records)
    print(f"[excel] {path}: leídos {len(records)} · nuevos {res['nuevos']} · "
          f"actualizados {res['actualizados']}")
    return res


def sync_woo(conn=None, desde: str | None = None) -> dict:
    conn = conn or get_conn()
    client = WooClient()
    if client.dry_run:
        print("[woo] sin credenciales (WOO_URL/WOO_KEY/WOO_SECRET): dry-run, nada que importar")
        return {"nuevos": 0, "actualizados": 0}
    records = client.importar_pedidos(desde=desde)
    res = upsert_alumnos(conn, records)
    print(f"[woo] pedidos {len(records)} · nuevos {res['nuevos']} · actualizados {res['actualizados']}")
    return res


def sync_hubspot_contactos(conn=None) -> dict:
    conn = conn or get_conn()
    client = HubSpotClient()
    if client.dry_run:
        print("[hubspot] sin HUBSPOT_TOKEN: dry-run, no se importan contactos")
        return {"contacto": 0}
    records = client.importar_contactos()
    res = upsert_alumnos(conn, records)
    print(f"[hubspot] contactos {len(records)} · enriquecidos {res['contacto']}")
    return res


def push_estados(conn=None, hoy: date | None = None) -> dict:
    """Empuja a HubSpot el estado de morosidad de cada alumno con email (solo si cambió)."""
    conn = conn or get_conn()
    client = HubSpotClient()
    panel = cargar_panel(conn, hoy=hoy)
    enviados = sin_cambio = sin_contacto = 0
    for _, r in panel.iterrows():
        email = r.get("email")
        if not email:
            sin_contacto += 1
            continue
        valor = estado_hubspot(r["estado"])
        if r.get("hubspot_estado") == valor:
            sin_cambio += 1
            continue
        resultado = client.push_estado(email, valor)
        if resultado in ("ok", "dry-run"):
            conn.execute(
                "UPDATE alumnos SET hubspot_estado = ?, hubspot_sync_fecha = ? WHERE id = ?",
                (valor, (hoy or date.today()).isoformat(), int(r["id"])))
            enviados += 1
        else:
            sin_contacto += 1
    conn.commit()
    modo = "dry-run" if client.dry_run else "en vivo"
    print(f"[push-estados] {modo}: enviados {enviados} · sin cambio {sin_cambio} · "
          f"sin contacto {sin_contacto}")
    return {"enviados": enviados, "sin_cambio": sin_cambio, "sin_contacto": sin_contacto}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return
    cmd, *args = argv
    conn = get_conn()
    if cmd == "excel" and args:
        sync_excel(args[0], conn)
    elif cmd == "woo":
        sync_woo(conn)
    elif cmd == "hubspot-contactos":
        sync_hubspot_contactos(conn)
    elif cmd == "push-estados":
        push_estados(conn)
    elif cmd == "all":
        sync_woo(conn)
        sync_hubspot_contactos(conn)
        push_estados(conn)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
