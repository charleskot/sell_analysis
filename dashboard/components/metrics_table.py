"""Listing ranking table with conditional formatting."""
import pandas as pd
import streamlit as st


CONDITION_LABELS = {
    "nuevo": "Nuevo",
    "buen_estado": "Buen estado",
    "reformar": "Para reformar",
    "desconocido": "Desconocido",
}

PORTAL_ICONS = {
    "idealista": "🔵",
    "fotocasa": "🟠",
    "pisos": "🟢",
    "habitaclia": "🟣",
}


def score_color(score: float) -> str:
    if score >= 75:
        return "background-color: #1a7a1a; color: white"
    elif score >= 60:
        return "background-color: #5c7a1a; color: white"
    elif score >= 45:
        return "background-color: #7a6a1a; color: white"
    else:
        return "background-color: #7a1a1a; color: white"


def render_table(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No hay anuncios que coincidan con los filtros seleccionados.")
        return

    display_df = pd.DataFrame({
        "Score": df["investment_score"].fillna(0).round(0).astype(int),
        "Portal": df["portal"].map(lambda x: f"{PORTAL_ICONS.get(x, '')} {x.title()}"),
        "Ciudad": df["city"].str.title(),
        "Barrio": df["district"].str.title().fillna("—"),
        "Precio": df["price"].map(lambda x: f"{x:,.0f}€" if pd.notna(x) else "—"),
        "m²": df["area_m2"].map(lambda x: f"{x:.0f}" if pd.notna(x) else "—"),
        "Hab": df["rooms"].fillna(0).astype(int).map(lambda x: str(x) if x > 0 else "—"),
        "€/m²": df["price_per_m2"].map(lambda x: f"{x:,.0f}" if pd.notna(x) else "—"),
        "Rent. Bruta": df["gross_yield_pct"].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—"),
        "Rent. Neta": df["net_yield_pct"].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—"),
        "Alquiler/mes": df["estimated_monthly_rent"].map(lambda x: f"{x:,.0f}€" if pd.notna(x) else "—"),
        "Payback": df["payback_years"].map(lambda x: f"{x:.0f} años" if pd.notna(x) else "—"),
        "Estado": df["condition"].map(lambda x: CONDITION_LABELS.get(x, x or "—")),
        "URL": df["url"],
    })

    st.dataframe(
        display_df.drop(columns=["URL"]),
        use_container_width=True,
        height=500,
        column_config={
            "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100),
            "Rent. Bruta": st.column_config.TextColumn("Rent. Bruta"),
            "Rent. Neta": st.column_config.TextColumn("Rent. Neta"),
        },
    )

    st.caption(f"Total anuncios: {len(df)}")


def render_listing_detail(row: pd.Series) -> None:
    """Expanded detail view for a single listing."""
    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric("Precio", f"{row.get('price', 0):,.0f}€")
        st.metric("Superficie", f"{row.get('area_m2', 0):.0f} m²")
        st.metric("Habitaciones", str(row.get("rooms", "—")))

    with col2:
        st.metric("Rentabilidad Bruta", f"{row.get('gross_yield_pct', 0):.1f}%")
        st.metric("Rentabilidad Neta", f"{row.get('net_yield_pct', 0):.1f}%")
        st.metric("Alquiler estimado", f"{row.get('estimated_monthly_rent', 0):,.0f}€/mes")

    with col3:
        st.metric("Inversión total", f"{row.get('total_investment', 0):,.0f}€")
        st.metric("Payback", f"{row.get('payback_years', 0):.0f} años")
        st.metric("Score", f"{row.get('investment_score', 0):.0f}/100")

    if row.get("url"):
        st.link_button("Ver anuncio completo", row["url"])

    breakdown = row.get("score_breakdown") or {}
    if breakdown:
        st.subheader("Desglose del Score")
        bd_data = {
            "Factor": ["Rentabilidad bruta", "Precio/m² vs zona", "Ubicación", "Estado", "ROI neto"],
            "Puntuación": [
                breakdown.get("gross_yield_score", 0),
                breakdown.get("price_per_m2_score", 0),
                breakdown.get("location_score", 0),
                breakdown.get("condition_score", 0),
                breakdown.get("roi_score", 0),
            ],
        }
        st.dataframe(pd.DataFrame(bd_data), use_container_width=True, hide_index=True)
