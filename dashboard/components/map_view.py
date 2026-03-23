"""Folium map with listings colored by investment score."""
import pandas as pd
import streamlit as st


def score_to_color(score: float) -> str:
    """Red (0) → Yellow (50) → Green (100)."""
    if score >= 75:
        return "#2ecc71"
    elif score >= 60:
        return "#f39c12"
    elif score >= 45:
        return "#e67e22"
    else:
        return "#e74c3c"


def render_map(df: pd.DataFrame) -> None:
    try:
        import folium
        from streamlit_folium import st_folium
    except ImportError:
        st.warning("Instala `folium` y `streamlit-folium` para ver el mapa.")
        return

    if df.empty:
        st.info("No hay datos con coordenadas para mostrar en el mapa.")
        return

    map_df = df.dropna(subset=["latitude", "longitude"])
    if map_df.empty:
        st.info("Los anuncios actuales no tienen coordenadas GPS. Se irán añadiendo con el scraping.")
        return

    center_lat = map_df["latitude"].mean()
    center_lon = map_df["longitude"].mean()

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles="CartoDB positron",
    )

    for _, row in map_df.iterrows():
        score = row.get("investment_score") or 0
        price = row.get("price") or 0
        area = row.get("area_m2") or 0
        gross_yield = row.get("gross_yield_pct") or 0
        city = str(row.get("city") or "").title()
        district = str(row.get("district") or "").title()
        url = row.get("url", "")

        popup_html = f"""
        <div style="min-width:200px">
            <b>Score: {score:.0f}/100</b><br>
            {district}, {city}<br>
            <b>{price:,.0f}€</b> | {area:.0f}m²<br>
            Rentabilidad: <b>{gross_yield:.1f}%</b><br>
            <a href="{url}" target="_blank">Ver anuncio</a>
        </div>
        """

        folium.CircleMarker(
            location=[row["latitude"], row["longitude"]],
            radius=max(5, min(15, score / 8)),
            color=score_to_color(score),
            fill=True,
            fill_color=score_to_color(score),
            fill_opacity=0.8,
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=f"Score {score:.0f} | {price:,.0f}€",
        ).add_to(m)

    st_folium(m, use_container_width=True, height=550)
