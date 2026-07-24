"""Lógica de negocio: estados de morosidad, aplicación de pagos y alarmas."""
from datetime import date, timedelta

import pandas as pd

HIGH_TICKET_MIN = 2000          # € — por debajo se considera low ticket
DIAS_VENCE_PRONTO = 7           # aviso previo al vencimiento
DIAS_SIN_GESTION = 7            # días sin actividad sobre un moroso -> alarma

# Orden de severidad para ordenar/pintar el panel
ESTADOS = [
    "Moroso +90d", "Moroso 61-90d", "Moroso 31-60d", "Moroso 1-30d",
    "Vence pronto", "Al día", "Financiado (Nemuru)", "Completado", "Sin plan",
]


def cargar_panel(conn, hoy: date | None = None) -> pd.DataFrame:
    """Devuelve un DataFrame con una fila por alumno y su situación de cobro."""
    hoy = hoy or date.today()
    hoy_iso = hoy.isoformat()

    alumnos = pd.read_sql_query("SELECT * FROM alumnos", conn)
    cuotas = pd.read_sql_query("SELECT * FROM cuotas", conn)
    ultima_act = pd.read_sql_query(
        "SELECT alumno_id, MAX(fecha) AS ultima_gestion, "
        "COUNT(*) AS n_gestiones FROM actividades GROUP BY alumno_id", conn)

    if alumnos.empty:
        return pd.DataFrame()

    cuotas["pendiente"] = (cuotas["importe"] - cuotas["importe_pagado"]).clip(lower=0)
    cuotas["vencida"] = (cuotas["fecha_vencimiento"] < hoy_iso) & (cuotas["pendiente"] > 0.01)

    agg = cuotas.groupby("alumno_id").agg(
        total_plan=("importe", "sum"),
        total_pagado=("importe_pagado", "sum"),
        deuda_vencida=("pendiente", lambda s: s[cuotas.loc[s.index, "vencida"]].sum()),
        saldo_pendiente=("pendiente", "sum"),
        n_cuotas=("numero", "count"),
        n_vencidas=("vencida", "sum"),
    )
    # fecha de la cuota vencida más antigua sin pagar -> días de retraso
    vencidas = cuotas[cuotas["vencida"]].groupby("alumno_id")["fecha_vencimiento"].min()
    agg["primera_vencida"] = vencidas
    # próxima cuota pendiente no vencida
    proximas = cuotas[(cuotas["pendiente"] > 0.01) & (cuotas["fecha_vencimiento"] >= hoy_iso)]
    agg["proximo_vencimiento"] = proximas.groupby("alumno_id")["fecha_vencimiento"].min()
    prox_importe = proximas.sort_values("fecha_vencimiento").groupby("alumno_id")["pendiente"].first()
    agg["proximo_importe"] = prox_importe

    df = alumnos.merge(agg, left_on="id", right_index=True, how="left")
    df = df.merge(ultima_act, left_on="id", right_on="alumno_id", how="left")

    df["dias_retraso"] = df["primera_vencida"].apply(
        lambda v: (hoy - date.fromisoformat(v)).days if isinstance(v, str) else 0)

    def estado(r):
        if r["tipo_pago"] == "Nemuru" and (r["saldo_pendiente"] or 0) <= 0.01:
            return "Financiado (Nemuru)"
        if pd.isna(r["n_cuotas"]) or r["n_cuotas"] == 0:
            return "Sin plan"
        if (r["saldo_pendiente"] or 0) <= 0.01:
            return "Completado"
        d = r["dias_retraso"]
        if d > 90:
            return "Moroso +90d"
        if d > 60:
            return "Moroso 61-90d"
        if d > 30:
            return "Moroso 31-60d"
        if d > 0:
            return "Moroso 1-30d"
        prox = r["proximo_vencimiento"]
        if isinstance(prox, str) and (date.fromisoformat(prox) - hoy).days <= DIAS_VENCE_PRONTO:
            return "Vence pronto"
        return "Al día"

    df["estado"] = df.apply(estado, axis=1)
    df["estado"] = pd.Categorical(df["estado"], categories=ESTADOS, ordered=True)
    df["high_ticket"] = df["precio"] >= HIGH_TICKET_MIN
    for col in ["total_plan", "total_pagado", "deuda_vencida", "saldo_pendiente", "proximo_importe"]:
        df[col] = df[col].fillna(0)
    return df.sort_values(["estado", "dias_retraso"], ascending=[True, False])


def registrar_pago(conn, alumno_id: int, fecha: date, importe: float,
                   metodo: str = "", nota: str = ""):
    """Registra un pago y lo aplica en cascada a las cuotas pendientes más antiguas."""
    conn.execute(
        "INSERT INTO pagos (alumno_id, fecha, importe, metodo, nota) VALUES (?,?,?,?,?)",
        (alumno_id, fecha.isoformat(), importe, metodo, nota),
    )
    restante = importe
    cuotas = conn.execute(
        "SELECT id, importe, importe_pagado FROM cuotas "
        "WHERE alumno_id = ? AND importe_pagado < importe - 0.01 "
        "ORDER BY fecha_vencimiento, numero", (alumno_id,)).fetchall()
    for c in cuotas:
        if restante <= 0.01:
            break
        hueco = c["importe"] - c["importe_pagado"]
        aplicado = min(hueco, restante)
        conn.execute(
            "UPDATE cuotas SET importe_pagado = importe_pagado + ?, fecha_pago = ? WHERE id = ?",
            (aplicado, fecha.isoformat(), c["id"]),
        )
        restante -= aplicado
    conn.commit()
    return restante  # sobrante no aplicado (pago mayor que la deuda)


def registrar_actividad(conn, alumno_id: int, tipo: str, nota: str,
                        fecha: date | None = None, fecha_compromiso: date | None = None):
    conn.execute(
        "INSERT INTO actividades (alumno_id, fecha, tipo, nota, fecha_compromiso) VALUES (?,?,?,?,?)",
        (alumno_id, (fecha or date.today()).isoformat(), tipo, nota,
         fecha_compromiso.isoformat() if fecha_compromiso else None),
    )
    conn.commit()


def generar_alarmas(conn, panel: pd.DataFrame, hoy: date | None = None) -> pd.DataFrame:
    """Alarmas accionables para la persona de recobros, ordenadas por prioridad.

    - CRÍTICA: deuda vencida hace más de 60 días.
    - VENCIDO SIN GESTIÓN: cuota impagada y sin actividad en DIAS_SIN_GESTION días.
    - PROMESA INCUMPLIDA: prometió pagar, la fecha pasó y sigue debiendo.
    - VENCE PRONTO: cuota vence en los próximos DIAS_VENCE_PRONTO días (recordatorio).
    """
    hoy = hoy or date.today()
    alarmas = []
    if panel.empty:
        return pd.DataFrame(columns=["prioridad", "tipo", "alumno_id", "alumno", "detalle"])

    promesas = pd.read_sql_query(
        "SELECT alumno_id, MAX(fecha_compromiso) AS compromiso FROM actividades "
        "WHERE tipo = 'promesa_pago' AND fecha_compromiso IS NOT NULL GROUP BY alumno_id", conn)
    promesas = promesas.set_index("alumno_id")["compromiso"].to_dict()

    for _, r in panel.iterrows():
        deuda = r["deuda_vencida"]
        if deuda > 0.01:
            dias = int(r["dias_retraso"])
            compromiso = promesas.get(r["id"])
            if compromiso and compromiso < hoy.isoformat():
                alarmas.append((1, "🔴 Promesa incumplida", r["id"], r["nombre"],
                                f"Prometió pagar el {compromiso} y sigue debiendo {deuda:,.0f}€ ({dias} días de retraso)"))
            elif dias > 60:
                alarmas.append((1, "🔴 Crítica +60d", r["id"], r["nombre"],
                                f"{deuda:,.0f}€ vencidos hace {dias} días — valorar escalado"))
            else:
                ug = r.get("ultima_gestion")
                sin_gestion = not isinstance(ug, str) or \
                    (hoy - date.fromisoformat(ug)).days > DIAS_SIN_GESTION
                if sin_gestion:
                    alarmas.append((2, "🟠 Vencido sin gestión", r["id"], r["nombre"],
                                    f"{deuda:,.0f}€ vencidos ({dias} días) y sin gestión en los últimos {DIAS_SIN_GESTION} días"))
                else:
                    alarmas.append((3, "🟡 Vencido en gestión", r["id"], r["nombre"],
                                    f"{deuda:,.0f}€ vencidos ({dias} días), última gestión {ug}"))
        elif r["estado"] == "Vence pronto":
            alarmas.append((4, "🔵 Vence pronto", r["id"], r["nombre"],
                            f"Cuota de {r['proximo_importe']:,.0f}€ vence el {r['proximo_vencimiento']}"))

    df = pd.DataFrame(alarmas, columns=["prioridad", "tipo", "alumno_id", "alumno", "detalle"])
    return df.sort_values(["prioridad", "alumno"]).reset_index(drop=True)
