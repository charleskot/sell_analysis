# Panel de Recobros

Panel para que una persona de recobros dé seguimiento a las matrículas
**high ticket** de behalf desde el momento de la matriculación: control de
plazos de cobro, detección de morosidad, alarmas y registro de la gestión
(llamadas, promesas de pago, cobros).

Está construido como un módulo autónomo (SQLite + Streamlit, sin dependencias
nuevas) para poder integrarse después dentro del panel de Versa.

## Cómo se ejecuta

```bash
streamlit run recobros/app.py
```

La primera vez importa `alumnos.csv` automáticamente y genera el plan de cuotas
de cada alumno en `data/recobros.db` (base de datos propia, separada del
scraping y fuera de git).

## Qué hace

- **KPIs**: saldo pendiente de cobro, deuda vencida, alumnos en mora, cuotas
  que vencen esta semana y cobrado en el mes en curso.
- **Tabla de alumnos** con estado de morosidad, días de retraso, deuda vencida,
  próximo vencimiento y última gestión. Filtrable por estado, tipo de pago,
  edición y nombre.
- **Alarmas** priorizadas y accionables: promesas incumplidas, morosos críticos
  (+60 días), vencidos sin gestión reciente y cuotas que vencen pronto.
- **Ficha por alumno**: datos de contacto editables con enlaces directos para
  llamar, escribir por WhatsApp o email; plan de cuotas, historial de pagos y de
  gestiones, con formularios para **registrar un pago** (se aplica en cascada a
  las cuotas pendientes más antiguas) y **registrar una gestión** (con fecha de
  compromiso para las promesas de pago). Permite **regenerar el plan** si se
  renegocian los plazos.
- **Analíticas de cartera**: distribución por estado de morosidad (aging),
  deuda por comercial y cobros por mes.
- **Exportación**: la lista filtrada de alumnos se descarga en CSV.

## Modelo de negocio reflejado (high vs low ticket)

- `high_ticket = precio ≥ 2000 €` (configurable en `logic.py`). El panel
  arranca filtrado a high ticket, que es donde hay plazos que recobrar.
- Reglas de generación del plan de cuotas (`importer.py`), ajustables por alumno:
  - **Contado**: sin plazos que recobrar (ingreso ya cobrado).
  - **Nemuru**: financiación externa; solo se registra el pago inicial, el resto
    lo cobra la financiera y no genera cuotas propias.
  - **Plazos / Contado Plazos**: cuota 0 = pago inicial (cobrado en la
    matrícula); el resto se reparte en cuotas mensuales iguales.

## Estados de morosidad

`Al día` · `Vence pronto` (≤7 días) · `Moroso 1-30d` · `Moroso 31-60d` ·
`Moroso 61-90d` · `Moroso +90d` · `Completado` · `Financiado (Nemuru)` ·
`Sin plan`. Se recalculan en cada carga con la fecha actual.

## Estructura

| Archivo | Responsabilidad |
|---------|-----------------|
| `db.py` | Esquema SQLite y conexión (`alumnos`, `cuotas`, `pagos`, `actividades`). |
| `importer.py` | Importación idempotente de `alumnos.csv` y generación de planes. |
| `logic.py` | Estados, cálculo del panel, aplicación de pagos, alarmas, contacto y analíticas. |
| `app.py` | Interfaz Streamlit. |
| `requirements.txt` | Dependencias del módulo (`pandas`, `streamlit`). |

Tests en `tests/test_recobros.py` (16 casos): `pytest tests/test_recobros.py`.

## Integración en Versa

La lógica (`logic.py`) y los datos (`db.py`) no dependen de Streamlit, así que
pueden reutilizarse tras un backend/API. Para integrarlo, exponer
`cargar_panel`, `registrar_pago`, `registrar_actividad` y `generar_alarmas`
sobre la misma base de datos.

> Nota: `alumnos.csv` llega hasta enero de 2021, por lo que con la fecha de hoy
> todas las cuotas históricas aparecen como morosas. Con datos actuales de
> behalf los estados se distribuirán de forma realista.
