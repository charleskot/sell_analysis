"""Tests de la lógica del panel de recobros (sin Streamlit)."""
from datetime import date

import pytest

from recobros.db import get_conn
from recobros.importer import generar_plan, n_cuotas_estimado
from recobros.logic import (
    actualizar_contacto, cargar_panel, cobros_mensuales, generar_alarmas,
    registrar_actividad, registrar_pago, resumen_aging, resumen_por_comercial,
)


@pytest.fixture
def conn(tmp_path):
    return get_conn(tmp_path / "test.db")


def _alta(conn, alumno_id, nombre, tipo_pago, precio, primer_pago,
          fecha, comercial="Ana", n_cuotas=None):
    conn.execute(
        "INSERT INTO alumnos (id, nombre, fecha_matricula, canal, comercial, tipo_pago, "
        "primer_pago, precio, edicion, tipo_cli) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (alumno_id, nombre, fecha.isoformat(), "Direct", comercial, tipo_pago,
         primer_pago, precio, "DTM0419", "B2C"))
    generar_plan(conn, alumno_id, precio, primer_pago, fecha, tipo_pago, n_cuotas=n_cuotas)


# ── Generación de planes ────────────────────────────────────────────────────

def test_n_cuotas_estimado():
    assert n_cuotas_estimado(5000, 500) == 9
    assert n_cuotas_estimado(3000, 0) == 10          # sin pago inicial -> default
    assert n_cuotas_estimado(500, 500) == 0          # nada que financiar
    assert n_cuotas_estimado(50000, 100) == 12       # acotado a 12


def test_plan_contado_sin_cuotas_pendientes(conn):
    _alta(conn, 1, "Contado Cliente", "Contado", 5000, 5000, date(2024, 1, 1))
    cuotas = conn.execute("SELECT * FROM cuotas WHERE alumno_id = 1").fetchall()
    assert len(cuotas) == 1
    assert cuotas[0]["importe_pagado"] == cuotas[0]["importe"]  # ya cobrada


def test_plan_plazos_reparte_resto(conn):
    _alta(conn, 1, "Plazos Cliente", "Plazos", 5000, 500, date(2024, 1, 1), n_cuotas=9)
    cuotas = conn.execute("SELECT * FROM cuotas WHERE alumno_id = 1 ORDER BY numero").fetchall()
    assert cuotas[0]["numero"] == 0 and cuotas[0]["importe"] == 500  # pago inicial
    plazos = cuotas[1:]
    assert len(plazos) == 9
    assert sum(c["importe"] for c in plazos) == pytest.approx(4500, abs=0.01)


def test_plan_nemuru_solo_pago_inicial(conn):
    _alta(conn, 1, "Nemuru Cliente", "Nemuru", 5000, 200, date(2024, 1, 1))
    cuotas = conn.execute("SELECT * FROM cuotas WHERE alumno_id = 1").fetchall()
    assert len(cuotas) == 1 and cuotas[0]["numero"] == 0  # el resto lo cobra la financiera


# ── Estados y panel ─────────────────────────────────────────────────────────

def test_estado_moroso_por_cuota_vencida(conn):
    _alta(conn, 1, "Moroso", "Plazos", 5000, 500, date(2020, 1, 1), n_cuotas=9)
    panel = cargar_panel(conn, hoy=date(2024, 1, 1))
    fila = panel[panel["id"] == 1].iloc[0]
    assert str(fila["estado"]).startswith("Moroso")
    assert fila["deuda_vencida"] > 0
    assert fila["dias_retraso"] > 90


def test_estado_al_dia_futuro(conn):
    _alta(conn, 1, "Al dia", "Plazos", 5000, 500, date(2024, 1, 1), n_cuotas=9)
    # justo tras la matrícula, ninguna cuota mensual ha vencido aún
    panel = cargar_panel(conn, hoy=date(2024, 1, 2))
    fila = panel[panel["id"] == 1].iloc[0]
    assert fila["deuda_vencida"] == 0
    assert str(fila["estado"]) in ("Al día", "Vence pronto")


def test_high_ticket_flag(conn):
    _alta(conn, 1, "Low", "Contado", 500, 500, date(2024, 1, 1))
    _alta(conn, 2, "High", "Plazos", 5000, 500, date(2024, 1, 1))
    panel = cargar_panel(conn, hoy=date(2024, 6, 1))
    assert not panel[panel["id"] == 1].iloc[0]["high_ticket"]
    assert panel[panel["id"] == 2].iloc[0]["high_ticket"]


# ── Pagos ───────────────────────────────────────────────────────────────────

def test_pago_se_aplica_en_cascada(conn):
    _alta(conn, 1, "Pagador", "Plazos", 5000, 500, date(2020, 1, 1), n_cuotas=9)
    # resto 4500 en 9 cuotas de 500; pago de 1200 cubre 2 cuotas y deja 200 en la 3ª
    sobrante = registrar_pago(conn, 1, date(2024, 1, 1), 1200)
    assert sobrante == 0
    cuotas = conn.execute(
        "SELECT importe, importe_pagado FROM cuotas WHERE alumno_id = 1 AND numero > 0 "
        "ORDER BY fecha_vencimiento").fetchall()
    assert cuotas[0]["importe_pagado"] == 500
    assert cuotas[1]["importe_pagado"] == 500
    assert cuotas[2]["importe_pagado"] == pytest.approx(200)


def test_pago_mayor_que_deuda_devuelve_sobrante(conn):
    _alta(conn, 1, "Sobrepago", "Plazos", 5000, 500, date(2020, 1, 1), n_cuotas=9)
    sobrante = registrar_pago(conn, 1, date(2024, 1, 1), 999999)
    assert sobrante == pytest.approx(999999 - 4500)
    panel = cargar_panel(conn, hoy=date(2024, 1, 2))
    assert panel[panel["id"] == 1].iloc[0]["saldo_pendiente"] == pytest.approx(0, abs=0.01)


def test_pago_completo_marca_completado(conn):
    _alta(conn, 1, "Completo", "Plazos", 5000, 500, date(2020, 1, 1), n_cuotas=9)
    registrar_pago(conn, 1, date(2024, 1, 1), 4500)
    panel = cargar_panel(conn, hoy=date(2024, 1, 2))
    assert str(panel[panel["id"] == 1].iloc[0]["estado"]) == "Completado"


# ── Alarmas ─────────────────────────────────────────────────────────────────

def test_alarma_promesa_incumplida(conn):
    _alta(conn, 1, "Promete", "Plazos", 5000, 500, date(2020, 1, 1), n_cuotas=9)
    registrar_actividad(conn, 1, "promesa_pago", "paga el día 10",
                        fecha=date(2023, 12, 1), fecha_compromiso=date(2023, 12, 10))
    panel = cargar_panel(conn, hoy=date(2024, 1, 1))
    alarmas = generar_alarmas(conn, panel, hoy=date(2024, 1, 1))
    assert (alarmas["tipo"] == "🔴 Promesa incumplida").any()


def test_alarma_vencido_sin_gestion(conn):
    _alta(conn, 1, "SinGestion", "Plazos", 5000, 500, date(2023, 10, 1), n_cuotas=9)
    panel = cargar_panel(conn, hoy=date(2023, 11, 15))
    alarmas = generar_alarmas(conn, panel, hoy=date(2023, 11, 15))
    assert (alarmas["alumno_id"] == 1).any()


def test_sin_morosos_sin_alarmas_criticas(conn):
    _alta(conn, 1, "Buen pagador", "Contado", 5000, 5000, date(2024, 1, 1))
    panel = cargar_panel(conn, hoy=date(2024, 1, 2))
    alarmas = generar_alarmas(conn, panel, hoy=date(2024, 1, 2))
    assert alarmas.empty


# ── Contacto y analíticas ───────────────────────────────────────────────────

def test_actualizar_contacto(conn):
    _alta(conn, 1, "Contacto", "Plazos", 5000, 500, date(2024, 1, 1))
    actualizar_contacto(conn, 1, "+34600111222", "a@b.com", "prefiere tardes")
    row = conn.execute("SELECT telefono, email, notas FROM alumnos WHERE id = 1").fetchone()
    assert row["telefono"] == "+34600111222"
    assert row["email"] == "a@b.com"
    assert row["notas"] == "prefiere tardes"


def test_resumen_aging_y_comercial(conn):
    _alta(conn, 1, "M1", "Plazos", 5000, 500, date(2020, 1, 1), comercial="Ana", n_cuotas=9)
    _alta(conn, 2, "M2", "Plazos", 4000, 400, date(2020, 1, 1), comercial="Ana", n_cuotas=9)
    panel = cargar_panel(conn, hoy=date(2024, 1, 1))
    aging = resumen_aging(panel)
    assert aging["alumnos"].sum() == 2
    por_com = resumen_por_comercial(panel)
    assert por_com.iloc[0]["comercial"] == "Ana"
    assert por_com.iloc[0]["alumnos"] == 2


def test_cobros_mensuales(conn):
    _alta(conn, 1, "Pagos", "Plazos", 5000, 500, date(2020, 1, 1), n_cuotas=9)
    registrar_pago(conn, 1, date(2024, 1, 15), 500)
    registrar_pago(conn, 1, date(2024, 1, 20), 500)
    registrar_pago(conn, 1, date(2024, 2, 5), 500)
    cobros = cobros_mensuales(conn)
    enero = cobros[cobros["mes"] == "2024-01"]["cobrado"].iloc[0]
    assert enero == pytest.approx(1000)
