"""Panel de recobros — Streamlit.

Ejecutar:  streamlit run recobros/app.py
Primera vez: importa automáticamente alumnos.csv y genera los planes de cuotas.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import streamlit as st

from recobros.db import get_conn
from recobros.importer import importar_csv, generar_plan, n_cuotas_estimado
from recobros.logic import (
    ETAPA_DEFECTO, ETAPAS_RECOBRO, HIGH_TICKET_MIN, actualizar_contacto,
    asignar_gestor, cargar_panel, cobros_mensuales, embudo_recobros,
    generar_alarmas, registrar_actividad, registrar_pago, registrar_pronto_pago,
    resumen_aging, resumen_por_comercial, set_etapa,
)
from recobros.sync import sync_excel

st.set_page_config(page_title="Panel de Recobros", page_icon="💶", layout="wide")

TIPOS_ACTIVIDAD = ["llamada", "whatsapp", "email", "promesa_pago", "comentario", "otro"]
COLOR_ESTADO = {
    "Moroso +90d": "🔴", "Moroso 61-90d": "🔴", "Moroso 31-60d": "🟠",
    "Moroso 1-30d": "🟠", "Vence pronto": "🔵", "Al día": "🟢",
    "Financiado (Nemuru)": "⚪", "Completado": "✅", "Sin plan": "⚫",
}


@st.cache_resource
def conexion():
    conn = get_conn()
    if conn.execute("SELECT COUNT(*) FROM alumnos").fetchone()[0] == 0:
        importar_csv()
    return conn


def eur(x) -> str:
    return f"{x:,.0f}€".replace(",", ".")


def _v(valor, default=""):
    """Devuelve default si el valor es None o NaN (pandas convierte NULL en NaN)."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return default
    return valor


def render_kpis(panel: pd.DataFrame):
    activos = panel[panel["saldo_pendiente"] > 0.01]
    morosos = panel[panel["estado"].astype(str).str.startswith("Moroso")]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Saldo pendiente de cobro", eur(activos["saldo_pendiente"].sum()))
    c2.metric("Deuda vencida", eur(morosos["deuda_vencida"].sum()),
              delta=f"{len(morosos)} alumnos", delta_color="inverse")
    c3.metric("Alumnos en mora", len(morosos))
    c4.metric("Vencen esta semana", int((panel["estado"] == "Vence pronto").sum()))
    pagos_mes = pd.read_sql_query(
        "SELECT COALESCE(SUM(importe),0) s FROM pagos WHERE strftime('%Y-%m', fecha) = strftime('%Y-%m','now')",
        conexion())["s"][0]
    c5.metric("Cobrado este mes", eur(pagos_mes))


def _fmt_etapa(e) -> str:
    return str(e or ETAPA_DEFECTO).replace("_", " ").capitalize()


def render_tabla(panel: pd.DataFrame):
    col1, col2, col3, col4, col5 = st.columns([2, 2, 2, 2, 3])
    estados_sel = col1.multiselect("Estado", panel["estado"].astype(str).unique().tolist())
    etapas_sel = col2.multiselect("Etapa recobro", ETAPAS_RECOBRO, format_func=_fmt_etapa)
    tipos_sel = col3.multiselect("Tipo de pago", sorted(panel["tipo_pago"].dropna().unique()))
    ediciones_sel = col4.multiselect("Edición", sorted(panel["edicion"].dropna().unique()))
    busqueda = col5.text_input("Buscar alumno", placeholder="Nombre...")

    df = panel
    if estados_sel:
        df = df[df["estado"].astype(str).isin(estados_sel)]
    if etapas_sel:
        df = df[df["etapa_recobro"].fillna("pendiente_contactar").isin(etapas_sel)]
    if tipos_sel:
        df = df[df["tipo_pago"].isin(tipos_sel)]
    if ediciones_sel:
        df = df[df["edicion"].isin(ediciones_sel)]
    if busqueda:
        df = df[df["nombre"].str.contains(busqueda, case=False, na=False)]

    vista = df[["id", "nombre", "estado", "etapa_recobro", "gestor_recobro", "dias_retraso",
                "deuda_vencida", "saldo_pendiente", "proximo_vencimiento", "proximo_importe",
                "ultima_gestion", "tipo_pago", "edicion", "comercial", "precio"]].copy()
    vista["estado"] = vista["estado"].astype(str).map(lambda e: f"{COLOR_ESTADO.get(e,'')} {e}")
    vista["etapa_recobro"] = vista["etapa_recobro"].map(_fmt_etapa)
    vista["gestor_recobro"] = vista["gestor_recobro"].fillna("")
    vista.columns = ["ID", "Alumno", "Estado", "Etapa", "Gestor", "Días retraso",
                     "Deuda vencida €", "Saldo pendiente €", "Próx. vencimiento",
                     "Próx. cuota €", "Última gestión", "Tipo pago", "Edición",
                     "Comercial", "Precio €"]
    st.dataframe(vista, use_container_width=True, hide_index=True, height=520)
    st.caption(f"{len(df)} alumnos · deuda vencida filtrada: {eur(df['deuda_vencida'].sum())}")
    st.download_button("⬇️ Exportar esta lista (CSV)", vista.to_csv(index=False).encode("utf-8"),
                       file_name="recobros_lista.csv", mime="text/csv")


def render_alarmas(conn, panel: pd.DataFrame):
    alarmas = generar_alarmas(conn, panel)
    if alarmas.empty:
        st.success("Sin alarmas activas 🎉")
        return
    resumen = alarmas["tipo"].value_counts()
    st.write(" · ".join(f"**{t}**: {n}" for t, n in resumen.items()))
    st.dataframe(
        alarmas[["tipo", "alumno_id", "alumno", "detalle"]].rename(
            columns={"tipo": "Alarma", "alumno_id": "ID", "alumno": "Alumno", "detalle": "Detalle"}),
        use_container_width=True, hide_index=True, height=480)


def render_analiticas(conn, panel: pd.DataFrame):
    st.subheader("Cartera por estado de morosidad")
    aging = resumen_aging(panel)
    if not aging.empty:
        c1, c2 = st.columns([3, 2])
        with c1:
            st.bar_chart(aging.set_index("estado")["saldo_pendiente"], height=300)
        with c2:
            tabla = aging.copy()
            tabla["saldo_pendiente"] = tabla["saldo_pendiente"].map(eur)
            tabla["deuda_vencida"] = tabla["deuda_vencida"].map(eur)
            tabla.columns = ["Estado", "Alumnos", "Saldo pendiente", "Deuda vencida"]
            st.dataframe(tabla, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Deuda por comercial")
    por_com = resumen_por_comercial(panel)
    if por_com.empty:
        st.caption("Sin deuda pendiente.")
    else:
        tabla = por_com.copy()
        tabla["deuda_vencida"] = tabla["deuda_vencida"].map(eur)
        tabla["saldo_pendiente"] = tabla["saldo_pendiente"].map(eur)
        tabla.columns = ["Comercial", "Alumnos", "Deuda vencida", "Saldo pendiente"]
        st.dataframe(tabla, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Cobros por mes")
    cobros = cobros_mensuales(conn)
    if cobros.empty:
        st.caption("Aún no hay pagos registrados en el panel.")
    else:
        st.bar_chart(cobros.set_index("mes")["cobrado"], height=280)


def render_embudo(panel: pd.DataFrame):
    st.subheader("Embudo de recobros")
    st.caption("Etapas del caso que mueve el gestor. Medido en dinero, no en nº de casos "
               "— los casos ya cobrados se quedan en su etapa.")
    emb = embudo_recobros(panel)
    if emb.empty:
        st.info("No hay alumnos con los filtros actuales.")
        return
    emb_vis = emb.copy()
    emb_vis["etapa"] = emb_vis["etapa"].str.replace("_", " ").str.capitalize()
    st.bar_chart(emb_vis.set_index("etapa")["deuda_vencida"], height=300)
    tabla = emb.copy()
    tabla["etapa"] = tabla["etapa"].str.replace("_", " ").str.capitalize()
    tabla["deuda_vencida"] = tabla["deuda_vencida"].map(eur)
    tabla["recobrado"] = tabla["recobrado"].map(eur)
    tabla.columns = ["Etapa", "Casos", "Deuda vencida", "Recobrado"]
    st.dataframe(tabla, use_container_width=True, hide_index=True)


def render_ficha(conn, panel: pd.DataFrame):
    opciones = {f"[{r['id']}] {r['nombre']} — {COLOR_ESTADO.get(str(r['estado']),'')} {r['estado']}": r["id"]
                for _, r in panel.iterrows()}
    if not opciones:
        st.info("No hay alumnos con los filtros actuales.")
        return
    sel = st.selectbox("Alumno", list(opciones.keys()))
    alumno_id = opciones[sel]
    r = panel[panel["id"] == alumno_id].iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Precio matrícula", eur(r["precio"]))
    c2.metric("Pagado", eur(r["total_pagado"]))
    c3.metric("Saldo pendiente", eur(r["saldo_pendiente"]))
    c4.metric("Deuda vencida", eur(r["deuda_vencida"]),
              delta=f"{int(r['dias_retraso'])} días" if r["dias_retraso"] else None,
              delta_color="inverse")
    st.caption(f"Matrícula: {r['fecha_matricula']} · {r['tipo_pago']} · {r['edicion']} · "
               f"Comercial: {r['comercial']} · Canal: {r['canal']}")

    # Funnel de cobro: etapa (la mueve el gestor) + asignación
    et1, et2 = st.columns([2, 2])
    etapa_actual = r.get("etapa_recobro") or ETAPA_DEFECTO
    nueva_etapa = et1.selectbox(
        "Etapa de recobro", ETAPAS_RECOBRO,
        index=ETAPAS_RECOBRO.index(etapa_actual) if etapa_actual in ETAPAS_RECOBRO else 0,
        format_func=lambda e: e.replace("_", " ").capitalize(), key=f"et_{alumno_id}")
    if nueva_etapa != etapa_actual:
        set_etapa(conn, alumno_id, nueva_etapa)
        st.rerun()
    gestor_actual = _v(r.get("gestor_recobro"), "")
    nuevo_gestor = et2.text_input("Gestor asignado", value=gestor_actual, key=f"ges_{alumno_id}")
    if nuevo_gestor != gestor_actual:
        asignar_gestor(conn, alumno_id, nuevo_gestor)
        st.rerun()

    tel = (r.get("telefono") or "").strip() if isinstance(r.get("telefono"), str) else ""
    mail = (r.get("email") or "").strip() if isinstance(r.get("email"), str) else ""
    enlaces = []
    if tel:
        enlaces.append(f"[📞 Llamar](tel:{tel})")
        enlaces.append(f"[💬 WhatsApp](https://wa.me/{tel.lstrip('+').replace(' ', '')})")
    if mail:
        enlaces.append(f"[✉️ Email](mailto:{mail})")
    if enlaces:
        st.markdown(" · ".join(enlaces))

    with st.expander("📇 Datos de contacto", expanded=not (tel or mail)):
        with st.form(f"contacto_{alumno_id}"):
            c_tel = st.text_input("Teléfono", value=tel, placeholder="+34...")
            c_mail = st.text_input("Email", value=mail)
            c_notas = st.text_area("Notas del alumno",
                                   value=(r.get("notas") or "") if isinstance(r.get("notas"), str) else "")
            if st.form_submit_button("Guardar contacto"):
                actualizar_contacto(conn, alumno_id, c_tel, c_mail, c_notas)
                st.success("Contacto actualizado.")
                st.rerun()

    col_izq, col_der = st.columns([3, 2])

    with col_izq:
        st.subheader("Plan de cuotas")
        cuotas = pd.read_sql_query(
            "SELECT numero, fecha_vencimiento, importe, importe_pagado, fecha_pago "
            "FROM cuotas WHERE alumno_id = ? ORDER BY numero", conn, params=(alumno_id,))
        if cuotas.empty:
            st.info("Sin plan de cuotas (contado o sin importe).")
        else:
            cuotas["estado"] = cuotas.apply(
                lambda c: "✅ Pagada" if c["importe_pagado"] >= c["importe"] - 0.01
                else ("🟠 Parcial" if c["importe_pagado"] > 0
                      else ("🔴 Vencida" if c["fecha_vencimiento"] < date.today().isoformat() else "⏳ Pendiente")),
                axis=1)
            cuotas.columns = ["Nº", "Vencimiento", "Importe €", "Pagado €", "Fecha pago", "Estado"]
            st.dataframe(cuotas, use_container_width=True, hide_index=True)

        with st.expander("♻️ Regenerar plan (renegociación)"):
            st.caption("Sustituye las cuotas pendientes por un plan nuevo. Úsalo si se renegocian los plazos.")
            n_def = n_cuotas_estimado(float(r["precio"]), float(r["primer_pago"]))
            n = st.number_input("Nº de cuotas", 1, 24, max(n_def, 1), key=f"n_{alumno_id}")
            primera = st.date_input("Primera cuota vence el", date.today(), key=f"f_{alumno_id}")
            if st.button("Regenerar plan", key=f"regen_{alumno_id}"):
                generar_plan(conn, alumno_id, float(r["precio"]), float(r["primer_pago"]),
                             date.fromisoformat(r["fecha_matricula"]), r["tipo_pago"],
                             n_cuotas=int(n), primera_cuota=primera)
                st.success("Plan regenerado.")
                st.rerun()

        st.subheader("Historial de pagos")
        pagos = pd.read_sql_query(
            "SELECT fecha, importe, metodo, nota FROM pagos WHERE alumno_id = ? ORDER BY fecha DESC",
            conn, params=(alumno_id,))
        if pagos.empty:
            st.caption("Sin pagos registrados manualmente.")
        else:
            pagos.columns = ["Fecha", "Importe €", "Método", "Nota"]
            st.dataframe(pagos, use_container_width=True, hide_index=True)

    with col_der:
        st.subheader("💶 Registrar pago")
        with st.form(f"pago_{alumno_id}", clear_on_submit=True):
            f_pago = st.date_input("Fecha", date.today())
            imp = st.number_input("Importe €", 0.0, step=50.0,
                                  value=float(r["proximo_importe"] or r["deuda_vencida"] or 0))
            metodo = st.selectbox("Método", ["transferencia", "tarjeta", "stripe", "efectivo", "otro"])
            nota_p = st.text_input("Nota", placeholder="Referencia, observaciones...")
            if st.form_submit_button("Registrar pago", type="primary") and imp > 0:
                sobrante = registrar_pago(conn, alumno_id, f_pago, imp, metodo, nota_p)
                if sobrante > 0.01:
                    st.warning(f"Pago aplicado. Sobrante sin cuota a la que aplicar: {eur(sobrante)}")
                st.rerun()

        st.subheader("🏷️ Descuento pronto pago")
        saldo = float(_v(r.get("saldo_pendiente"), 0))
        pp_importe = _v(r.get("pronto_pago_importe"), 0)
        if pp_importe:
            lim_val = _v(r.get("pronto_pago_limite"), "")
            lim = f" · válido hasta {lim_val}" if lim_val else ""
            st.info(f"Oferta activa: {_v(r.get('pronto_pago_pct'), 0):.0f}% dto → "
                    f"pagar {eur(pp_importe)}{lim}")
        if saldo <= 0:
            st.caption("Sin saldo pendiente: nada que ofrecer.")
        else:
            with st.form(f"pp_{alumno_id}"):
                pct = st.slider("Descuento por pagar ahora (%)", 0, 50,
                                int(_v(r.get("pronto_pago_pct"), 0)), 5)
                importe_pactado = round(saldo * (1 - pct / 100), 2)
                st.metric("Pagaría ahora", eur(importe_pactado),
                          delta=f"-{eur(saldo - importe_pactado)}", delta_color="inverse")
                limite = st.date_input("Válido hasta", value=None, key=f"ppl_{alumno_id}")
                if st.form_submit_button("Guardar oferta") and pct > 0:
                    registrar_pronto_pago(conn, alumno_id, pct, importe_pactado, limite)
                    st.rerun()

        st.subheader("📞 Registrar gestión")
        with st.form(f"act_{alumno_id}", clear_on_submit=True):
            tipo = st.selectbox("Tipo", TIPOS_ACTIVIDAD)
            nota_a = st.text_area("Nota", placeholder="Qué se habló, resultado...")
            compromiso = st.date_input("Fecha compromiso de pago (solo promesas)", value=None)
            if st.form_submit_button("Guardar gestión"):
                registrar_actividad(conn, alumno_id, tipo, nota_a,
                                    fecha_compromiso=compromiso if tipo == "promesa_pago" else None)
                st.rerun()

        st.subheader("Historial de gestiones")
        acts = pd.read_sql_query(
            "SELECT fecha, tipo, nota, fecha_compromiso FROM actividades "
            "WHERE alumno_id = ? ORDER BY fecha DESC, id DESC", conn, params=(alumno_id,))
        if acts.empty:
            st.caption("Sin gestiones registradas.")
        else:
            acts.columns = ["Fecha", "Tipo", "Nota", "Compromiso"]
            st.dataframe(acts, use_container_width=True, hide_index=True, height=260)


def main():
    conn = conexion()
    st.title("💶 Panel de Recobros")
    st.caption("Seguimiento de matrículas high ticket: plazos, morosidad, gestiones y pagos. "
               "Los estados se recalculan automáticamente con la fecha de hoy.")

    solo_ht = st.sidebar.toggle(f"Solo high ticket (≥ {HIGH_TICKET_MIN}€)", value=True)
    solo_activos = st.sidebar.toggle("Solo con saldo pendiente", value=True)
    st.sidebar.divider()
    if st.sidebar.button("🔄 Reimportar alumnos.csv"):
        n = importar_csv()
        st.sidebar.success(f"{n} alumnos nuevos importados")
        st.rerun()

    st.sidebar.subheader("Subir matrículas (Excel/CSV)")
    st.sidebar.caption("Histórico pre-ecommerce. Se fusiona por email sin duplicar.")
    subida = st.sidebar.file_uploader("Archivo", type=["xlsx", "xls", "csv"],
                                      label_visibility="collapsed")
    if subida is not None and st.sidebar.button("Importar archivo"):
        tmp = Path(st.session_state.get("_tmpdir", ".")) / f"_subida_{subida.name}"
        tmp.write_bytes(subida.getbuffer())
        try:
            res = sync_excel(str(tmp), conn)
            st.sidebar.success(f"Nuevos {res['nuevos']} · actualizados {res['actualizados']}")
        finally:
            tmp.unlink(missing_ok=True)
        st.rerun()

    panel = cargar_panel(conn)
    if panel.empty:
        st.warning("No hay alumnos. Comprueba que alumnos.csv existe y reimporta desde la barra lateral.")
        return
    if solo_ht:
        panel = panel[panel["high_ticket"]]
    vista_panel = panel[panel["saldo_pendiente"] > 0.01] if solo_activos else panel

    render_kpis(panel)
    st.divider()

    tab_alumnos, tab_alarmas, tab_ficha, tab_embudo, tab_analiticas = st.tabs(
        ["📋 Alumnos", "🚨 Alarmas", "👤 Ficha / Gestión", "🔻 Embudo", "📊 Analíticas"])
    with tab_alumnos:
        render_tabla(vista_panel)
    with tab_alarmas:
        render_alarmas(conn, vista_panel)
    with tab_ficha:
        render_ficha(conn, vista_panel)
    with tab_embudo:
        render_embudo(vista_panel)
    with tab_analiticas:
        render_analiticas(conn, panel)


if __name__ == "__main__":
    main()
