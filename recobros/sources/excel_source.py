"""Ingesta de subidas de Excel/CSV (matrículas del histórico pre-ecommerce).

Acepta .xlsx, .xls y .csv. Los nombres de columna son flexibles: se mapean por
sinónimos habituales (mayúsculas/acentos incluidos). Fechas en dd/mm/aaaa o ISO.
"""
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .base import AlumnoRecord

# sinónimo (en minúsculas) -> campo normalizado
COLUMNAS = {
    "id": "id", "id alumno": "id",
    "alumno": "nombre", "nombre": "nombre", "name": "nombre", "nombre completo": "nombre",
    "fecha": "fecha_matricula", "fecha matricula": "fecha_matricula",
    "fecha de matricula": "fecha_matricula", "fecha alta": "fecha_matricula",
    "canal": "canal", "source": "canal", "original source": "canal",
    "comercial": "comercial", "closer": "comercial", "vendedor": "comercial",
    "tipo de pago": "tipo_pago", "tipo pago": "tipo_pago", "forma de pago": "tipo_pago",
    "#1 pago": "primer_pago", "primer pago": "primer_pago", "pago inicial": "primer_pago",
    "entrada": "primer_pago",
    "precio": "precio", "importe": "precio", "total": "precio", "precio total": "precio",
    "edicion": "edicion", "edición": "edicion", "curso": "edicion", "programa": "edicion",
    "tipocli": "tipo_cli", "tipo cli": "tipo_cli", "tipo cliente": "tipo_cli",
    "email": "email", "correo": "email", "e-mail": "email", "mail": "email",
    "telefono": "telefono", "teléfono": "telefono", "movil": "telefono",
    "móvil": "telefono", "phone": "telefono",
}


def _num(v) -> float:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    if isinstance(v, str):
        v = v.replace("€", "").replace(".", "").replace(",", ".").strip()
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _fecha(v) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            continue
    return None


def leer_archivo(path: Path | str) -> list[AlumnoRecord]:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=object)
    else:
        df = pd.read_csv(path, dtype=object)

    # renombra columnas conocidas al nombre normalizado
    renombres = {c: COLUMNAS[c.strip().lower()] for c in df.columns
                 if c.strip().lower() in COLUMNAS}
    df = df.rename(columns=renombres)

    records: list[AlumnoRecord] = []
    for _, row in df.iterrows():
        nombre = row.get("nombre")
        if nombre is None or (isinstance(nombre, float) and pd.isna(nombre)):
            nombre = ""
        nombre = str(nombre).strip()
        fecha = _fecha(row.get("fecha_matricula"))
        if not nombre or nombre.lower() == "nan" or fecha is None:
            continue  # fila sin datos mínimos: se ignora
        tipo_pago = (str(row.get("tipo_pago")).strip()
                     if row.get("tipo_pago") not in (None, "") else "Contado")
        rid = row.get("id")
        try:
            rid = int(rid) if rid not in (None, "") and not pd.isna(rid) else None
        except (ValueError, TypeError):
            rid = None
        records.append(AlumnoRecord(
            id=rid,
            nombre=nombre,
            fecha_matricula=fecha,
            tipo_pago=tipo_pago,
            precio=_num(row.get("precio")),
            primer_pago=_num(row.get("primer_pago")),
            email=(str(row["email"]).strip() if row.get("email") not in (None, "") else None),
            telefono=(str(row["telefono"]).strip() if row.get("telefono") not in (None, "") else None),
            canal=(str(row["canal"]).strip() if row.get("canal") not in (None, "") else None),
            comercial=(str(row["comercial"]).strip() if row.get("comercial") not in (None, "") else None),
            edicion=(str(row["edicion"]).strip() if row.get("edicion") not in (None, "") else None),
            tipo_cli=(str(row["tipo_cli"]).strip() if row.get("tipo_cli") not in (None, "") else None),
            origen="excel",
        ))
    return records
