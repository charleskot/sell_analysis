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
    notas TEXT
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


def get_conn(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
