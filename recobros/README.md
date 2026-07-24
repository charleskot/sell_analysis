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
- **Funnel de recobros (7 etapas)**: cada caso tiene una etapa que mueve el
  gestor desde la ficha (`pendiente_contactar` → `contactado` → `en_negociacion`
  → `compromiso_pago` → `cierre_satisfactorio`, con salidas `ilocalizado` y
  `cierre_fallido`). Se asigna un **gestor** a cada caso. Etapa y gestor son
  filtrables y visibles en la tabla.
- **Embudo de recobros**: los casos por etapa, medido en dinero (deuda vencida y
  recobrado), no en nº de casos; los casos ya cobrados permanecen en su etapa.
- **Analíticas de cartera**: distribución por estado de morosidad (aging),
  deuda por comercial y cobros por mes.
- **Exportación**: la lista filtrada de alumnos se descarga en CSV.

> El **estado de morosidad** (por tiempo, automático) y la **etapa de recobro**
> (el trabajo del caso, la mueve el gestor) son dos ejes distintos y
> complementarios, igual que en el sistema de Hofmann.

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

## Fuentes de datos e integraciones

El panel se alimenta de tres fuentes que se consolidan **por email** (sin
duplicar a la misma persona) en la misma base de datos:

| Fuente | Módulo | Rol |
|--------|--------|-----|
| **Excel/CSV histórico** | `sources/excel_source.py` | Matrículas de antes del ecommerce nuevo. Se suben desde la barra lateral del panel o por CLI. Columnas con nombres flexibles (sinónimos). |
| **WooCommerce** | `sources/woocommerce.py` | Ventas, plazos y pagos en vivo (REST API v3). |
| **HubSpot** | `sources/hubspot.py` | Enriquece los datos de contacto (tel/email) y **recibe el estado de morosidad** (CRM API v3). |

`sources/base.py` define el registro normalizado (`AlumnoRecord`) y la fusión
por email (`upsert_alumnos`). `sync.py` orquesta el flujo y trae una CLI:

```bash
python -m recobros.sync excel data/matriculas_2023.xlsx  # subir histórico
python -m recobros.sync woo                                # importar pedidos Woo
python -m recobros.sync hubspot-contactos                 # enriquecer contactos
python -m recobros.sync push-estados                      # escribir morosidad en HubSpot
python -m recobros.sync all                               # woo + contactos + push
```

Sin credenciales, los pasos de Woo/HubSpot funcionan en **dry-run** (no llaman a
la API), de modo que el flujo completo se puede probar sin claves.

### Configuración (variables de entorno — ver `.env.example`)

| Variable | Uso |
|----------|-----|
| `HUBSPOT_TOKEN` | Token privado de la app de HubSpot. |
| `HUBSPOT_PROP_MOROSIDAD` | Nombre interno de la propiedad de contacto donde se escribe el estado (def: `estado_recobro`). |
| `WOO_URL`, `WOO_KEY`, `WOO_SECRET` | Credenciales de la tienda WooCommerce. |

**Pendiente de confirmar desde el proyecto `bihsales`** para pasar de dry-run a
producción: (1) las claves de arriba; (2) el nombre interno real y los valores
del desplegable de la propiedad de morosidad en HubSpot (mapeo en
`logic.ESTADO_HUBSPOT`); (3) qué plugin gestiona los plazos en WooCommerce, para
afinar `WooClient._tipo_pago_desde_pedido`.

## Estructura

| Archivo | Responsabilidad |
|---------|-----------------|
| `db.py` | Esquema SQLite y conexión (`alumnos`, `cuotas`, `pagos`, `actividades`). |
| `importer.py` | Importación de `alumnos.csv` y generación de planes de cuotas. |
| `logic.py` | Estados, cálculo del panel, pagos, alarmas, analíticas y mapeo a HubSpot. |
| `sources/` | Fuentes de datos (base, excel, woocommerce, hubspot). |
| `sync.py` | Orquestador de sincronización + CLI. |
| `app.py` | Interfaz Streamlit. |
| `requirements.txt` | Dependencias (`pandas`, `streamlit`, `requests`, `openpyxl`). |

Tests: `pytest tests/test_recobros.py tests/test_sources.py` (29 casos).

## Integración en Versa

La lógica (`logic.py`) y los datos (`db.py`) no dependen de Streamlit, así que
pueden reutilizarse tras un backend/API. Para integrarlo, exponer
`cargar_panel`, `registrar_pago`, `registrar_actividad` y `generar_alarmas`
sobre la misma base de datos.

> Nota: `alumnos.csv` llega hasta enero de 2021, por lo que con la fecha de hoy
> todas las cuotas históricas aparecen como morosas. Con datos actuales de
> behalf los estados se distribuirán de forma realista.
