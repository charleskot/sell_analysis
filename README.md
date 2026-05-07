# Auction Analyzer — Subastas inmobiliarias en Cataluña

App que cada día:

1. Rastrea plataformas de subastas inmobiliarias filtrando por las cuatro provincias catalanas (Barcelona, Girona, Lleida, Tarragona).
2. Guarda las subastas nuevas en SQLite.
3. Analiza cada nueva subasta con Claude (`claude-opus-4-7`): valor de mercado estimado, descuento, yield de alquiler, ROI de flip, riesgos legales, y un score 0-100.
4. Te envía un email con las que superan el umbral (por defecto score ≥ 60).

Plataformas incluidas:

- **subastas.boe.es** — subastas judiciales y notariales oficiales (la fuente principal y la más confiable).
- **addmeet.com** — subastas privadas.
- **idealista.com** — listings con flag de subasta. *Aviso: Idealista bloquea bots agresivamente; en producción usa su API o un proxy con Playwright.*
- **solvia, haya, servihabitat, aliseda** — inmuebles bancarios de adjudicación. *Estos son SPAs JavaScript; el scraper estático solo captura el subset SSR-renderizado.*

## Stack

- Python 3.11+ · FastAPI · SQLAlchemy 2 · SQLite
- Anthropic SDK (Claude Opus 4.7) con prompt caching y structured outputs
- httpx + BeautifulSoup para scraping
- APScheduler para la ejecución diaria
- Jinja2 + smtplib para el email

## Setup

```bash
cd sell_analysis
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edita .env con tu ANTHROPIC_API_KEY y credenciales SMTP
```

### Email con Gmail

1. Activa 2FA en tu cuenta Google.
2. Crea una "App password" en https://myaccount.google.com/apppasswords.
3. Pon esa contraseña en `SMTP_PASSWORD`.

## Uso

### Ejecutar una vez (manual)

```bash
python run_daily.py            # scrape + analiza + envía email
python run_daily.py --dry-run  # todo menos el envío
```

### Servidor (scheduler + API)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Esto:

- Inicializa la base de datos.
- Lanza APScheduler con un cron diario (hora configurable via `DAILY_RUN_HOUR`).
- Expone una API:
  - `GET /health`
  - `GET /auctions?source=boe&province=Barcelona&min_score=60&limit=50`
  - `GET /auctions/{id}`
  - `GET /reports/latest`
  - `GET /preview?min_score=60` — vista previa HTML del email del día
  - `POST /run` — dispara una corrida en background

### Como cron del sistema (alternativa al scheduler interno)

Si prefieres no tener un proceso siempre vivo:

```cron
0 8 * * * cd /ruta/a/sell_analysis && /ruta/a/.venv/bin/python run_daily.py >> /var/log/auctions.log 2>&1
```

## Configuración

Todo vive en `.env`. Las claves más útiles:

| Variable | Default | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | API key (https://console.anthropic.com/) |
| `ANALYSIS_MODEL` | `claude-opus-4-7` | Cambiable a `claude-sonnet-4-6` para abaratar |
| `TARGET_PROVINCES` | `08,17,25,43` | Provincias INE (08 Barcelona, 17 Girona, 25 Lleida, 43 Tarragona) |
| `MAX_PRICE_EUR` | `400000` | Filtra antes del LLM para no quemar tokens en mansiones |
| `MIN_OPPORTUNITY_SCORE` | `60` | Score mínimo para incluir en el email |
| `ENABLED_SCRAPERS` | `boe,addmeet,idealista,solvia,haya,servihabitat,aliseda` | Habilita/deshabilita fuentes |
| `DAILY_RUN_HOUR/MINUTE` | `8/0` | Hora del cron interno |

## Arquitectura

```
sell_analysis/
├── app/
│   ├── config.py           # Settings (pydantic-settings)
│   ├── db.py               # SQLAlchemy engine + session
│   ├── models.py           # Auction / Analysis / DailyReport
│   ├── scrapers/
│   │   ├── base.py         # BaseScraper + AuctionItem dataclass
│   │   ├── boe.py          # subastas.boe.es (más completo)
│   │   ├── addmeet.py
│   │   ├── idealista.py
│   │   ├── _bank_base.py   # base genérica para bancos
│   │   ├── solvia.py / haya.py / servihabitat.py / aliseda.py
│   ├── analyzer.py         # Claude con structured outputs + prompt caching
│   ├── notifier.py         # Render Jinja + envío SMTP
│   ├── pipeline.py         # Orquestador: scrape → upsert → analyze → email
│   ├── scheduler.py        # APScheduler cron
│   ├── main.py             # FastAPI
│   └── templates/email.html.j2
├── tests/test_boe_parser.py
├── run_daily.py
├── requirements.txt
└── .env.example
```

### Cómo funciona el análisis

`app/analyzer.py` envía cada subasta a Claude con:

- **System prompt cacheado** (`cache_control: ephemeral`): contiene las instrucciones de análisis. Como es constante para todas las subastas del día, las llamadas posteriores cuestan ~10× menos en tokens de input.
- **Adaptive thinking**: Claude decide cuánto razonar por subasta.
- **Structured outputs**: el JSON de salida está validado contra un Pydantic schema (`OpportunityAnalysis`), así que nunca recibes JSON malformado.

Salida (en español):

- `opportunity_score` 0-100
- `estimated_market_value_eur`, `estimated_discount_pct`
- `estimated_rental_yield_pct`, `estimated_flip_roi_pct`
- `summary`, `pros[]`, `cons[]`, `legal_risks[]`
- `recommendation`: `"comprar"` / `"investigar"` / `"descartar"`

## Mantenimiento de scrapers

Las páginas web cambian. Para diagnosticar:

```bash
python run_daily.py --dry-run 2>&1 | grep -i "scraper\|failed"
```

El scraper de **BOE** es el más estable (HTML del gobierno, estructura tabular).

Los scrapers de **bancos** (Solvia, Haya, etc.) son SPAs y la versión actual solo captura lo SSR. Para cobertura completa, sustituye `httpx.Client` por `playwright` en `_bank_base.py`.

**Idealista** bloquea bots — para producción seria usa su API o proxies residenciales.

## Tests

```bash
pytest
```

## Coste estimado de Claude

Por subasta (Opus 4.7, system cacheado, ~500 tokens de input variable + ~600 tokens de output):

- Input no cacheado: ~$0.0025
- Output: ~$0.015
- **~$0.018 por subasta analizada**

Si filtras a 30-50 subastas/día, son ~$0.50-$0.90/día. Bajar a `claude-sonnet-4-6` lo deja en ~$0.15-$0.27/día.

## Notas legales

- Asegúrate de respetar robots.txt y términos de servicio de cada plataforma.
- BOE es información pública.
- Los portales bancarios y privados pueden requerir consentimiento para scraping a escala — limita la frecuencia y usa el `User-Agent` configurable.
