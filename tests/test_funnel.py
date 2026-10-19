"""Tests del funnel de cobro, asignación de gestor, embudo y pronto pago."""
from datetime import date

import pytest

from recobros.db import get_conn
from recobros.logic import (
    ETAPAS_RECOBRO, asignar_gestor, cargar_panel, embudo_recobros,
    registrar_pago, registrar_pronto_pago, set_etapa,
)


@pytest.fixture
def conn(tmp_path):
    return get_conn(tmp_path / "test.db")


def _alumno(conn, aid, etapa="pendiente", gestor=None, precio=5000):
    conn.execute(
        "INSERT INTO alumnos (id, nombre, fecha_matricula, tipo_pago, precio, comercial, "
        "etapa_recobro, gestor_recobro) VALUES (?,?,?,?,?,?,?,?)",
        (aid, f"Alumno {aid}", "2024-01-01", "Plazos", precio, "Ana", etapa, gestor))
    conn.commit()


def _cuota(conn, aid, numero, vencimiento, importe, pagado, fecha_pago):
    conn.execute(
        "INSERT INTO cuotas (alumno_id, numero, fecha_vencimiento, importe, importe_pagado, "
        "fecha_pago) VALUES (?,?,?,?,?,?)",
        (aid, numero, vencimiento, importe, pagado, fecha_pago))
    conn.commit()


# ── Funnel de cobro (4 etapas, enfoque en cobrar) ───────────────────────────

def test_etapas_de_cobro():
    assert ETAPAS_RECOBRO == ["pendiente", "en_gestion", "cobrado", "incobrable"]


def test_set_etapa_valida(conn):
    _alumno(conn, 1)
    set_etapa(conn, 1, "en_gestion")
    assert conn.execute("SELECT etapa_recobro FROM alumnos WHERE id=1").fetchone()[0] == "en_gestion"


def test_set_etapa_invalida_lanza(conn):
    _alumno(conn, 1)
    with pytest.raises(ValueError):
        set_etapa(conn, 1, "en_negociacion")   # etapa del funnel antiguo, ya no válida


def test_asignar_gestor(conn):
    _alumno(conn, 1)
    asignar_gestor(conn, 1, "María Recobros")
    assert conn.execute("SELECT gestor_recobro FROM alumnos WHERE id=1").fetchone()[0] == "María Recobros"
    asignar_gestor(conn, 1, "")
    assert conn.execute("SELECT gestor_recobro FROM alumnos WHERE id=1").fetchone()[0] is None


def test_embudo_agrupa_por_etapa_en_dinero(conn):
    _alumno(conn, 1, etapa="cobrado")
    _cuota(conn, 1, 1, "2024-01-15", 2000, 2000, "2024-03-10")
    _alumno(conn, 2, etapa="pendiente")
    _cuota(conn, 2, 1, "2024-01-15", 1000, 0, None)
    panel = cargar_panel(conn, hoy=date(2024, 4, 1))
    emb = embudo_recobros(panel)
    cobrado = emb[emb["etapa"] == "cobrado"].iloc[0]
    assert cobrado["casos"] == 1 and cobrado["recobrado"] == 2000
    pendiente = emb[emb["etapa"] == "pendiente"].iloc[0]
    assert pendiente["deuda_vencida"] == 1000


# ── Migración: etapas antiguas se remapean al nuevo funnel ───────────────────

def test_migracion_remapea_etapas_antiguas(conn):
    # simula una base con el enum antiguo y fuerza la migración de nuevo
    conn.execute("UPDATE alumnos SET etapa_recobro='compromiso_pago' WHERE 1=0")  # no-op, tabla vacía
    conn.execute("INSERT INTO alumnos (id, nombre, fecha_matricula, tipo_pago, precio, etapa_recobro) "
                 "VALUES (1,'X','2024-01-01','Plazos',5000,'compromiso_pago')")
    conn.execute("INSERT INTO alumnos (id, nombre, fecha_matricula, tipo_pago, precio, etapa_recobro) "
                 "VALUES (2,'Y','2024-01-01','Plazos',5000,'cierre_satisfactorio')")
    conn.execute("INSERT INTO alumnos (id, nombre, fecha_matricula, tipo_pago, precio, etapa_recobro) "
                 "VALUES (3,'Z','2024-01-01','Plazos',5000,'cierre_fallido')")
    conn.commit()
    from recobros.db import _migrar
    _migrar(conn)
    filas = dict(conn.execute("SELECT id, etapa_recobro FROM alumnos").fetchall())
    assert filas[1] == "en_gestion"      # compromiso_pago -> en_gestion
    assert filas[2] == "cobrado"         # cierre_satisfactorio -> cobrado
    assert filas[3] == "incobrable"      # cierre_fallido -> incobrable


# ── Descuento por pronto pago ───────────────────────────────────────────────

def test_registrar_pronto_pago(conn):
    _alumno(conn, 1)
    _cuota(conn, 1, 1, "2024-01-15", 2000, 0, None)   # 2000 pendiente
    registrar_pronto_pago(conn, 1, 20, 1600, date(2024, 4, 30), "cierre exprés")
    row = conn.execute("SELECT pronto_pago_pct, pronto_pago_importe, pronto_pago_limite "
                       "FROM alumnos WHERE id=1").fetchone()
    assert row["pronto_pago_pct"] == 20
    assert row["pronto_pago_importe"] == 1600
    assert row["pronto_pago_limite"] == "2024-04-30"
    # queda registrado como gestión en el historial
    act = conn.execute("SELECT tipo FROM actividades WHERE alumno_id=1").fetchone()
    assert act["tipo"] == "pronto_pago"
