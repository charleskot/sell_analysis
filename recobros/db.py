"""Capa de datos del panel de recobros (SQLite, sin dependencias externas).

Base de datos propia (data/recobros.db) separada de la de scraping para que el
módulo pueda extraerse tal cual e integrarse en otro panel (Versa).
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "recobros.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alumnos (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    fecha_matricula TEXT NOT NULL,          -- ISO yyyy-mm-dd
    canal TEXT,
    comercial TEXT,
    tipo_pago TEXT NOT NULL,                -- Contado | Contado Plazos | Plazos | Nemuru
    primer_pago REAL DEFAULT 0,
    precio REAL DEFAULT 0,
    edicion TEXT,
    tipo_cli TEXT,
    telefono TEXT,
    email TEXT,
    notas TEXT,
    origen TEXT DEFAULT 'excel',            -- excel | woocommerce | hubspot
    ref_externa TEXT,                       -- id del pedido Woo / contacto HubSpot
    hubspot_estado TEXT,                    -- último estado de morosidad empujado a HubSpot
    hubspot_sync_fecha TEXT                 -- cuándo se empujó por última vez
);

CREATE TABLE IF NOT EXISTS cuotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
    numero INTEGER NOT NULL,                -- 0 = pago inicial
    fecha_vencimiento TEXT NOT NULL,        -- ISO yyyy-mm-dd
    importe REAL NOT NULL,
    importe_pagado REAL NOT NULL DEFAULT 0,
    fecha_pago TEXT,                        -- fecha del último pago aplicado
    UNIQUE (alumno_id, numero)
);

CREATE TABLE IF NOT EXISTS pagos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    importe REAL NOT NULL,
    metodo TEXT,                            -- transferencia, tarjeta, stripe...
    nota TEXT,
    creado_en TEXT NOT NULL DEFAULT (date('now'))
);

CREATE TABLE IF NOT EXISTS actividades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    tipo TEXT NOT NULL,                     -- llamada, email, whatsapp, promesa_pago, comentario, otro
    nota TEXT,
    fecha_compromiso TEXT,                  -- solo para promesa_pago: fecha en que promete pagar
    creado_en TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cuotas_alumno ON cuotas(alumno_id);
CREATE INDEX IF NOT EXISTS idx_pagos_alumno ON pagos(alumno_id);
CREATE INDEX IF NOT EXISTS idx_actividades_alumno ON actividades(alumno_id);
"""


# Columnas añadidas después de la primera versión: se aplican a bases ya creadas.
_MIGRACIONES = {
    "alumnos": {
        "origen": "TEXT DEFAULT 'excel'",
        "ref_externa": "TEXT",
        "hubspot_estado": "TEXT",
        "hubspot_sync_fecha": "TEXT",
    },
}


def _migrar(conn: sqlite3.Connection):
    for tabla, columnas in _MIGRACIONES.items():
        existentes = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabla})")}
        for col, definicion in columnas.items():
            if col not in existentes:
                conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {definicion}")
    conn.commit()


def get_conn(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrar(conn)
    return conn
