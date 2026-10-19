"""Modelo normalizado de alumno y consolidación por email.

Cualquier fuente convierte sus datos en `AlumnoRecord` y llama a `upsert_alumnos`.
La identidad es el **email** (en minúsculas y sin espacios): si ya existe un alumno
con ese email, se actualiza; si no, se inserta uno nuevo y se le genera su plan de
cuotas. Los datos de gestión (pagos, actividades, cuotas ya pagadas) nunca se pisan.
"""
from dataclasses import dataclass, field
from datetime import date

from ..importer import generar_plan


@dataclass
class AlumnoRecord:
    nombre: str
    fecha_matricula: date
    tipo_pago: str                      # Contado | Contado Plazos | Plazos | Nemuru
    precio: float
    primer_pago: float = 0.0
    email: str | None = None
    telefono: str | None = None
    canal: str | None = None
    comercial: str | None = None
    edicion: str | None = None
    tipo_cli: str | None = None
    origen: str = "excel"               # excel | woocommerce | hubspot
    ref_externa: str | None = None      # id de pedido Woo / contacto HubSpot
    # Solo para enriquecimiento de contacto (HubSpot): no crea alumno por sí solo.
    solo_contacto: bool = False
    id: int | None = None               # id explícito (Excel histórico con ID propio)
    _campos_contacto: tuple = field(default=("telefono", "email"), repr=False)


def normalizar_email(valor) -> str | None:
    if not valor or not isinstance(valor, str):
        return None
    v = valor.strip().lower()
    return v or None


def _siguiente_id(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM alumnos").fetchone()
    return int(row["n"])


def _buscar_por_email(conn, email: str):
    return conn.execute("SELECT * FROM alumnos WHERE lower(email) = ?", (email,)).fetchone()


def upsert_alumnos(conn, records: list[AlumnoRecord]) -> dict:
    """Consolida una lista de registros. Devuelve conteos {nuevos, actualizados, contacto}."""
    nuevos = actualizados = contacto = 0
    for r in records:
        email = normalizar_email(r.email)
        existente = _buscar_por_email(conn, email) if email else None

        if existente is None and r.id is not None:
            existente = conn.execute("SELECT * FROM alumnos WHERE id = ?", (r.id,)).fetchone()

        if existente is not None:
            # Actualiza contacto siempre; datos económicos solo desde fuentes de venta.
            campos = {"nombre": r.nombre or existente["nombre"],
                      "telefono": r.telefono or existente["telefono"],
                      "email": email or existente["email"]}
            if not r.solo_contacto:
                campos.update({
                    "canal": r.canal or existente["canal"],
                    "comercial": r.comercial or existente["comercial"],
                    "edicion": r.edicion or existente["edicion"],
                    "ref_externa": r.ref_externa or existente["ref_externa"],
                })
            sets = ", ".join(f"{k} = ?" for k in campos)
            conn.execute(f"UPDATE alumnos SET {sets} WHERE id = ?",
                         (*campos.values(), existente["id"]))
            contacto += 1 if r.solo_contacto else 0
            actualizados += 0 if r.solo_contacto else 1
            conn.commit()
            continue

        if r.solo_contacto:
            # Contacto de HubSpot sin matrícula conocida: no creamos alumno.
            continue

        nuevo_id = r.id if r.id is not None else _siguiente_id(conn)
        conn.execute(
            "INSERT INTO alumnos (id, nombre, fecha_matricula, canal, comercial, tipo_pago, "
            "primer_pago, precio, edicion, tipo_cli, telefono, email, origen, ref_externa) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nuevo_id, r.nombre, r.fecha_matricula.isoformat(), r.canal, r.comercial,
             r.tipo_pago, r.primer_pago, r.precio, r.edicion, r.tipo_cli,
             r.telefono, email, r.origen, r.ref_externa))
        generar_plan(conn, nuevo_id, r.precio, r.primer_pago, r.fecha_matricula, r.tipo_pago)
        nuevos += 1
        conn.commit()

    return {"nuevos": nuevos, "actualizados": actualizados, "contacto": contacto}
