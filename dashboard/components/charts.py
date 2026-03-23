"""Analytics charts for the dashboard."""
import pandas as pd
import streamlit as st


def render_yield_distribution(df: pd.DataFrame) -> None:
    if df.empty or "gross_yield_pct" not in df.columns:
        return

    data = df["gross_yield_pct"].dropna()
    if data.empty:
        return

    st.subheader("Distribución de Rentabilidad Bruta")
    hist_data = data.value_counts(bins=20).sort_index()
    hist_df = pd.DataFrame({"Rentabilidad (%)": hist_data.index.astype(str), "Anuncios": hist_data.values})
    st.bar_chart(data, use_container_width=True)


def render_price_per_m2_by_district(df: pd.DataFrame) -> None:
    if df.empty:
        return

    data = df.dropna(subset=["price_per_m2", "district"])
    if data.empty:
        return

    st.subheader("Precio/m² por Barrio")
    grouped = (
        data.groupby("district")["price_per_m2"]
        .mean()
        .sort_values(ascending=False)
        .head(15)
        .reset_index()
    )
    grouped.columns = ["Barrio", "€/m² medio"]
    grouped["Barrio"] = grouped["Barrio"].str.title()
    st.bar_chart(grouped.set_index("Barrio"), use_container_width=True)


def render_score_vs_yield(df: pd.DataFrame) -> None:
    if df.empty:
        return

    data = df.dropna(subset=["investment_score", "gross_yield_pct"])
    if data.empty:
        return

    st.subheader("Score vs Rentabilidad")
    try:
        import altair as alt
        chart = (
            alt.Chart(data)
            .mark_circle(size=60, opacity=0.7)
            .encode(
                x=alt.X("gross_yield_pct:Q", title="Rentabilidad Bruta (%)"),
                y=alt.Y("investment_score:Q", title="Score de Inversión"),
                color=alt.Color("city:N", title="Ciudad"),
                tooltip=["city", "district", "price", "gross_yield_pct", "investment_score", "area_m2"],
            )
            .interactive()
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        st.scatter_chart(data[["gross_yield_pct", "investment_score"]])


def render_portal_breakdown(df: pd.DataFrame) -> None:
    if df.empty:
        return

    st.subheader("Anuncios por Portal")
    counts = df["portal"].value_counts().reset_index()
    counts.columns = ["Portal", "Anuncios"]
    st.bar_chart(counts.set_index("Portal"), use_container_width=True)


def render_top_opportunities(df: pd.DataFrame, n: int = 10) -> None:
    if df.empty:
        return

    st.subheader(f"Top {n} Mejores Oportunidades")
    top = df.nlargest(n, "investment_score")[
        ["city", "district", "price", "area_m2", "gross_yield_pct", "investment_score", "url"]
    ].copy()
    top["city"] = top["city"].str.title()
    top["district"] = top["district"].str.title().fillna("—")
    top.columns = ["Ciudad", "Barrio", "Precio (€)", "m²", "Rent. Bruta (%)", "Score", "URL"]
    st.dataframe(top.drop(columns=["URL"]), use_container_width=True, hide_index=True)
