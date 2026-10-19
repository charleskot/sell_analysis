"""Fuentes de datos del panel de recobros.

Todas las fuentes (Excel histórico, WooCommerce, HubSpot) producen `AlumnoRecord`
normalizados y se consolidan con `upsert_alumnos`, que fusiona por email para no
duplicar a la misma persona entre sistemas.
"""
from .base import AlumnoRecord, upsert_alumnos, normalizar_email

__all__ = ["AlumnoRecord", "upsert_alumnos", "normalizar_email"]
