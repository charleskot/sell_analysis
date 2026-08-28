"""Tests de la capa de fuentes: fusión por email, Excel, conectores dry-run y sync."""
from datetime import date

import pytest

from recobros.db import get_conn
from recobros.logic import cargar_panel, estado_hubspot
from recobros.sources.base import AlumnoRecord, normalizar_email, upsert_alumnos
from recobros.sources.excel_source import leer_archivo
from recobros.sources.hubspot import HubSpotClient
from recobros.sources.woocommerce import WooClient
from recobros import sync


@pytest.fixture
def conn(tmp_path):
    return get_conn(tmp_path / "test.db")


def _rec(**kw):
    base = dict(nombre="Test", fecha_matricula=date(2024, 1, 1),
                tipo_pago="Plazos", precio=5000, primer_pago=500)
    base.update(kw)
    return AlumnoRecord(**base)


# ── Normalización / fusión por email ────────────────────────────────────────

def test_normalizar_email():
    assert normalizar_email("  Foo@Bar.COM ") == "foo@bar.com"
    assert normalizar_email("") is None
    assert normalizar_email(None) is None


def test_upsert_inserta_nuevo_y_genera_plan(conn):
    res = upsert_alumnos(conn, [_rec(email="a@b.com")])
    assert res["nuevos"] == 1
    cuotas = conn.execute("SELECT COUNT(*) c FROM cuotas").fetchone()["c"]
    assert cuotas > 0  # plan generado


def test_upsert_fusiona_por_email_sin_duplicar(conn):
    upsert_alumnos(conn, [_rec(email="Dup@b.com", telefono=None)])
    # segundo registro mismo email (distinta capitalización) -> actualiza, no duplica
    res = upsert_alumnos(conn, [_rec(email="dup@b.com", telefono="+34600")])
    assert res["actualizados"] == 1
    filas = conn.execute("SELECT COUNT(*) c FROM alumnos").fetchone()["c"]
    assert filas == 1
    tel = conn.execute("SELECT telefono FROM alumnos").fetchone()["telefono"]
    assert tel == "+34600"


def test_contacto_hubspot_no_crea_alumno(conn):
    # registro solo-contacto sin matrícula previa: no debe crear alumno
    res = upsert_alumnos(conn, [AlumnoRecord(
        nombre="Solo Contacto", fecha_matricula=None, tipo_pago="", precio=0,
        email="x@y.com", telefono="+34700", solo_contacto=True)])
    assert res["nuevos"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM alumnos").fetchone()["c"] == 0


def test_contacto_hubspot_enriquece_existente(conn):
    upsert_alumnos(conn, [_rec(email="z@y.com", telefono=None)])
    res = upsert_alumnos(conn, [AlumnoRecord(
        nombre="Z", fecha_matricula=None, tipo_pago="", precio=0,
        email="z@y.com", telefono="+34999", solo_contacto=True)])
    assert res["contacto"] == 1
    tel = conn.execute("SELECT telefono FROM alumnos").fetchone()["telefono"]
    assert tel == "+34999"


# ── Excel / CSV ─────────────────────────────────────────────────────────────

def test_leer_csv_flexible(tmp_path):
    csv = tmp_path / "m.csv"
    csv.write_text(
        "Nombre,Fecha,Forma de pago,Pago inicial,Importe,Correo\n"
        "Juan Pérez,15/03/2024,Plazos,600,4800,juan@mail.com\n"
        "Ana,2024-04-01,Contado,3000,3000,\n", encoding="utf-8")
    records = leer_archivo(csv)
    assert len(records) == 2
    juan = records[0]
    assert juan.nombre == "Juan Pérez"
    assert juan.fecha_matricula == date(2024, 3, 15)
    assert juan.tipo_pago == "Plazos"
    assert juan.precio == 4800 and juan.primer_pago == 600
    assert juan.email == "juan@mail.com"
    assert records[1].fecha_matricula == date(2024, 4, 1)  # ISO también


def test_leer_csv_ignora_filas_incompletas(tmp_path):
    csv = tmp_path / "m.csv"
    csv.write_text("Nombre,Fecha\n,15/03/2024\nPedro,\n", encoding="utf-8")
    assert leer_archivo(csv) == []


# ── Conectores en dry-run (sin credenciales) ────────────────────────────────

def test_hubspot_dry_run_sin_token(monkeypatch):
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    c = HubSpotClient()
    assert c.dry_run is True
    assert c.importar_contactos() == []
    assert c.push_estado("a@b.com", "moroso_90") == "dry-run"
    assert c.push_estado("", "moroso_90") == "sin-contacto"


def test_woo_dry_run_sin_claves(monkeypatch):
    for v in ("WOO_URL", "WOO_KEY", "WOO_SECRET"):
        monkeypatch.delenv(v, raising=False)
    c = WooClient()
    assert c.dry_run is True
    assert c.importar_pedidos() == []


def test_woo_tipo_pago_desde_pedido():
    assert WooClient._tipo_pago_desde_pedido({"payment_method": "sequra_xyz"}) == "Nemuru"
    assert WooClient._tipo_pago_desde_pedido(
        {"payment_method": "bacs", "meta_data": [{"key": "_deposit_amount", "value": "300"}]}) == "Plazos"
    assert WooClient._tipo_pago_desde_pedido({"payment_method": "card"}) == "Contado"


# ── Mapeo de estado y sync push (dry-run) ───────────────────────────────────

def test_estado_hubspot_mapeo():
    assert estado_hubspot("Moroso +90d") == "moroso_90"
    assert estado_hubspot("Al día") == "al_dia"
    assert estado_hubspot("Completado") == "completado"


def test_push_estados_dry_run_marca_en_db(conn, monkeypatch):
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    upsert_alumnos(conn, [_rec(email="moroso@b.com", fecha_matricula=date(2020, 1, 1))])
    res = sync.push_estados(conn, hoy=date(2024, 1, 1))
    assert res["enviados"] == 1
    fila = conn.execute("SELECT hubspot_estado FROM alumnos").fetchone()
    assert fila["hubspot_estado"] == "moroso_90"
    # segunda pasada: ya está marcado, no reenvía
    res2 = sync.push_estados(conn, hoy=date(2024, 1, 1))
    assert res2["enviados"] == 0 and res2["sin_cambio"] == 1


def test_push_estados_sin_email_no_envia(conn, monkeypatch):
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    upsert_alumnos(conn, [_rec(email=None)])
    res = sync.push_estados(conn, hoy=date(2024, 1, 1))
    assert res["enviados"] == 0 and res["sin_contacto"] == 1
