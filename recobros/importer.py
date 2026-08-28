"""Importa alumnos.csv y genera el plan de cuotas de cada matrícula.

Reglas de generación del plan (ajustables después por alumno desde el panel):
- Contado / precio 0 ......... sin cuotas (nada que recobrar).
- Nemuru ..................... financiación externa: solo se registra el pago
                               inicial; el resto lo cobra la financiera.
- Plazos / Contado Plazos .... cuota 0 = pago inicial (se asume cobrado en la
                               matrícula). El resto (precio - inicial) se
                               reparte en cuotas mensuales iguales. El nº de
                               cuotas se estima como resto / pago inicial
                               (acotado 1-12); si no hay pago inicial, 10.
"""
import csv
from datetime import date, datetime
from pathlib import Path

from .db import get_conn

CSV_PATH = Path(__file__).parent.parent / "alumnos.csv"

MESES_MAX = 12
MESES_DEFAULT = 10


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def n_cuotas_estimado(precio: float, primer_pago: float) -> int:
    resto = precio - primer_pago
    if resto <= 0:
        return 0
    if primer_pago <= 0:
        return MESES_DEFAULT
    return max(1, min(MESES_MAX, round(resto / primer_pago)))


def generar_plan(conn, alumno_id: int, precio: float, primer_pago: float,
                 fecha_matricula: date, tipo_pago: str,
                 n_cuotas: int | None = None, primera_cuota: date | None = None):
    """(Re)genera el plan de cuotas de un alumno. Borra el plan anterior."""
    conn.execute("DELETE FROM cuotas WHERE alumno_id = ?", (alumno_id,))

    if precio <= 0 or tipo_pago == "Contado":
        # Contado: una única "cuota" ya cobrada para reflejar el ingreso.
        if precio > 0:
            conn.execute(
                "INSERT INTO cuotas (alumno_id, numero, fecha_vencimiento, importe, importe_pagado, fecha_pago) "
                "VALUES (?, 0, ?, ?, ?, ?)",
                (alumno_id, fecha_matricula.isoformat(), precio, precio, fecha_matricula.isoformat()),
            )
        conn.commit()
        return

    # Pago inicial (se asume cobrado al matricularse)
    if primer_pago > 0:
        conn.execute(
            "INSERT INTO cuotas (alumno_id, numero, fecha_vencimiento, importe, importe_pagado, fecha_pago) "
            "VALUES (?, 0, ?, ?, ?, ?)",
            (alumno_id, fecha_matricula.isoformat(), primer_pago, primer_pago, fecha_matricula.isoformat()),
        )

    if tipo_pago == "Nemuru":
        # El resto lo gestiona la financiera: no generamos cuotas propias.
        conn.commit()
        return

    resto = precio - primer_pago
    n = n_cuotas if n_cuotas is not None else n_cuotas_estimado(precio, primer_pago)
    if resto > 0 and n > 0:
        importe = round(resto / n, 2)
        for i in range(1, n + 1):
            vence = _add_months(primera_cuota, i - 1) if primera_cuota else _add_months(fecha_matricula, i)
            # última cuota absorbe el redondeo
            imp = round(resto - importe * (n - 1), 2) if i == n else importe
            conn.execute(
                "INSERT INTO cuotas (alumno_id, numero, fecha_vencimiento, importe) VALUES (?, ?, ?, ?)",
                (alumno_id, i, vence.isoformat(), imp),
            )
    conn.commit()


def importar_csv(csv_path: Path = CSV_PATH, db_path=None):
    """Importación idempotente: solo inserta alumnos cuyo ID no exista aún."""
    conn = get_conn(db_path) if db_path else get_conn()
    existentes = {r["id"] for r in conn.execute("SELECT id FROM alumnos")}
    nuevos = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            alumno_id = int(row["ID"])
            if alumno_id in existentes:
                continue
            fecha = datetime.strptime(row["Fecha"], "%d/%m/%Y").date()
            primer_pago = float(row["#1 Pago"] or 0)
            precio = float(row["Precio"] or 0)
            tipo_pago = (row["Tipo de Pago"] or "").strip()
            conn.execute(
                "INSERT INTO alumnos (id, nombre, fecha_matricula, canal, comercial, tipo_pago, "
                "primer_pago, precio, edicion, tipo_cli) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (alumno_id, row["Alumno"].strip(), fecha.isoformat(), row["Canal"], row["Comercial"],
                 tipo_pago, primer_pago, precio, row["Edición"], row["TipoCli"]),
            )
            generar_plan(conn, alumno_id, precio, primer_pago, fecha, tipo_pago)
            nuevos += 1
    conn.commit()
    return nuevos


if __name__ == "__main__":
    n = importar_csv()
    print(f"Importados {n} alumnos nuevos")
