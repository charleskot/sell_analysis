"""Tests del funnel de recobros (7 etapas), asignación de gestor y embudo."""
from datetime import date

import pytest

from recobros.db import get_conn
from recobros.logic import (
    ETAPAS_RECOBRO, asignar_gestor, cargar_panel, embudo_recobros, set_etapa,
)


@pytest.fixture
def conn(tmp_path):
    return get_conn(tmp_path / "test.db")


def _alumno(conn, aid, etapa="pendiente_contactar", gestor=None):
    conn.execute(
        "INSERT INTO alumnos (id, nombre, fecha_matricula, tipo_pago, precio, comercial, "
        "etapa_recobro, gestor_recobro) VALUES (?,?,?,?,?,?,?,?)",
        (aid, f"Alumno {aid}", "2024-01-01", "Plazos", 5000, "Ana", etapa, gestor))
    conn.commit()


def _cuota(conn, aid, numero, vencimiento, importe, pagado, fecha_pago):
    conn.execute(
        "INSERT INTO cuotas (alumno_id, numero, fecha_vencimiento, importe, importe_pagado, "
        "fecha_pago) VALUES (?,?,?,?,?,?)",
        (aid, numero, vencimiento, importe, pagado, fecha_pago))
    conn.commit()


def test_siete_etapas_definidas():
    assert ETAPAS_RECOBRO == [
        "pendiente_contactar", "contactado", "en_negociacion", "compromiso_pago",
        "cierre_satisfactorio", "ilocalizado", "cierre_fallido"]


def test_set_etapa_valida(conn):
    _alumno(conn, 1)
    set_etapa(conn, 1, "en_negociacion")
    assert conn.execute("SELECT etapa_recobro FROM alumnos WHERE id=1").fetchone()[0] == "en_negociacion"


def test_set_etapa_invalida_lanza(conn):
    _alumno(conn, 1)
    with pytest.raises(ValueError):
        set_etapa(conn, 1, "no_existe")


def test_asignar_gestor(conn):
    _alumno(conn, 1)
    asignar_gestor(conn, 1, "María Recobros")
    assert conn.execute("SELECT gestor_recobro FROM alumnos WHERE id=1").fetchone()[0] == "María Recobros"
    asignar_gestor(conn, 1, "")   # vaciar -> NULL
    assert conn.execute("SELECT gestor_recobro FROM alumnos WHERE id=1").fetchone()[0] is None


def test_embudo_agrupa_por_etapa_en_dinero(conn):
    _alumno(conn, 1, etapa="compromiso_pago")
    _cuota(conn, 1, 1, "2024-01-15", 2000, 2000, "2024-03-10")
    _alumno(conn, 2, etapa="pendiente_contactar")
    _cuota(conn, 2, 1, "2024-01-15", 1000, 0, None)  # deuda vencida sin pagar
    panel = cargar_panel(conn, hoy=date(2024, 4, 1))
    emb = embudo_recobros(panel)
    compromiso = emb[emb["etapa"] == "compromiso_pago"].iloc[0]
    assert compromiso["casos"] == 1 and compromiso["recobrado"] == 2000
    pendiente = emb[emb["etapa"] == "pendiente_contactar"].iloc[0]
    assert pendiente["deuda_vencida"] == 1000


def test_embudo_vacio(conn):
    assert embudo_recobros(cargar_panel(conn)).empty
