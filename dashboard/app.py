"""Streamlit dashboard - main entry point."""
import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

st.set_page_config(
    page_title="Analizador Inmobiliario España",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)


def init_app():
    """Initialize DB connection for dashboard."""
    import yaml
    from models.db import init_engine, create_all_tables

    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    db_path = config["database"]["path"]
    if not Path(db_path).is_absolute():
        db_path = str(Path(__file__).parent.parent / db_path)

    init_engine(db_path)
    create_all_tables()
    return config


def main():
    config = init_app()

    # Import dashboard modules
    from dashboard.data_layer import get_ranked_listings, get_available_cities, get_stats_summary
    from dashboard.components.filters import render_filters
    from dashboard.components.metrics_table import render_table, render_listing_detail
    from dashboard.components.map_view import render_map
    from dashboard.components.charts import (
        render_yield_distribution,
        render_price_per_m2_by_district,
        render_score_vs_yield,
        render_portal_breakdown,
        render_top_opportunities,
    )

    # Header
    st.title("🏠 Analizador de Inversión Inmobiliaria")
    st.caption("Scraping en tiempo real de Idealista, Fotocasa, Pisos.com | Actualización automática cada 12h")

    # Sidebar filters
    available_cities = get_available_cities()
    filters = render_filters(available_cities)

    # Load data
    df = get_ranked_listings(
        cities=filters.cities or None,
        portals=filters.portals or None,
        price_min=filters.price_min,
        price_max=filters.price_max,
        min_yield_pct=filters.min_yield_pct,
        min_score=filters.min_score,
        min_m2=filters.min_m2,
        max_m2=filters.max_m2,
        min_rooms=filters.min_rooms,
        conditions=filters.conditions or None,
    )

    # KPI metrics row
    stats = get_stats_summary()
    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
    kpi1.metric("Anuncios totales", f"{stats['total']:,}")
    kpi2.metric("Anuncios filtrados", f"{len(df):,}")
    kpi3.metric("Precio medio", f"{stats['avg_price']:,.0f}€")
    kpi4.metric("Rent. media bruta", f"{stats['avg_yield']:.1f}%")
    kpi5.metric("Score medio", f"{stats['avg_score']:.0f}/100")

    st.divider()

    # Main tabs
    tab1, tab2, tab3, tab4 = st.tabs(["Ranking", "Mapa", "Analíticas", "Detalles"])

    with tab1:
        render_table(df)

    with tab2:
        render_map(df)

    with tab3:
        col1, col2 = st.columns(2)
        with col1:
            render_yield_distribution(df)
            render_score_vs_yield(df)
        with col2:
            render_price_per_m2_by_district(df)
            render_portal_breakdown(df)

        render_top_opportunities(df)

    with tab4:
        st.subheader("Detalle de anuncio")
        if df.empty:
            st.info("No hay datos. Ejecuta el scraper primero con: `python main.py scrape --once`")
        else:
            listing_options = {
                f"[{row['investment_score']:.0f}] {row['city'].title()} - {row.get('district', '').title()} - {row['price']:,.0f}€": idx
                for idx, row in df.iterrows()
                if pd.notna(row.get("investment_score"))
            }
            if listing_options:
                selected_label = st.selectbox("Selecciona un anuncio", list(listing_options.keys()))
                selected_idx = listing_options[selected_label]
                render_listing_detail(df.loc[selected_idx])


import pandas as pd  # needed for render_listing_detail

if __name__ == "__main__":
    main()
