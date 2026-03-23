"""Sidebar filter widgets for Streamlit dashboard."""
from dataclasses import dataclass, field

import streamlit as st


@dataclass
class FilterSpec:
    cities: list[str] = field(default_factory=list)
    portals: list[str] = field(default_factory=list)
    price_min: float = 0.0
    price_max: float = 2_000_000.0
    min_yield_pct: float = 0.0
    min_score: float = 0.0
    min_m2: float = 0.0
    max_m2: float = 500.0
    min_rooms: int = 0
    conditions: list[str] = field(default_factory=list)


def render_filters(available_cities: list[str]) -> FilterSpec:
    st.sidebar.header("Filtros")

    # City filter
    cities = st.sidebar.multiselect(
        "Ciudad",
        options=available_cities,
        default=[],
        help="Deja vacío para todas las ciudades",
    )

    # Portal filter
    portals = st.sidebar.multiselect(
        "Portal",
        options=["idealista", "fotocasa", "pisos", "habitaclia"],
        default=["idealista", "fotocasa", "pisos"],
    )

    st.sidebar.divider()
    st.sidebar.subheader("Precio")
    price_min, price_max = st.sidebar.slider(
        "Rango de precio (€)",
        min_value=0,
        max_value=2_000_000,
        value=(0, 2_000_000),
        step=5_000,
        format="%d€",
    )

    st.sidebar.divider()
    st.sidebar.subheader("Rentabilidad")
    min_yield = st.sidebar.slider(
        "Rentabilidad bruta mínima (%)",
        min_value=0.0,
        max_value=15.0,
        value=0.0,
        step=0.5,
    )

    min_score = st.sidebar.slider(
        "Score mínimo (0-100)",
        min_value=0,
        max_value=100,
        value=0,
        step=5,
    )

    st.sidebar.divider()
    st.sidebar.subheader("Características")
    m2_min, m2_max = st.sidebar.slider(
        "Superficie (m²)",
        min_value=0,
        max_value=500,
        value=(0, 500),
        step=5,
    )

    min_rooms = st.sidebar.selectbox(
        "Habitaciones mínimas",
        options=[0, 1, 2, 3, 4, 5],
        index=0,
    )

    conditions = st.sidebar.multiselect(
        "Estado",
        options=["nuevo", "buen_estado", "reformar", "desconocido"],
        default=[],
        help="Deja vacío para todos los estados",
    )

    return FilterSpec(
        cities=cities,
        portals=portals,
        price_min=float(price_min),
        price_max=float(price_max),
        min_yield_pct=float(min_yield),
        min_score=float(min_score),
        min_m2=float(m2_min),
        max_m2=float(m2_max),
        min_rooms=int(min_rooms),
        conditions=conditions,
    )
