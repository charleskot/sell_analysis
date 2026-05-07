# Auction Analyzer — Subastas inmobiliarias en Cataluña

App que cada día:

1. Rastrea plataformas de subastas inmobiliarias filtrando por las cuatro provincias catalanas (Barcelona, Girona, Lleida, Tarragona).
2. Guarda las subastas nuevas en SQLite.
3. Analiza cada nueva subasta con Claude (`claude-opus-4-7`): valor de mercado estimado, descuento, yield de alquiler, ROI de flip, riesgos legales, y un score 0-100.
4. **Te manda por Telegram** las que superan el umbral (por defecto score ≥ 60).

Plataformas incluidas:

- **subastas.boe.es** — subastas judiciales y notariales oficiales (la fuente principal y la más confiable).
- **addmeet.com** — subastas privadas.
- **solvia, haya, servihabitat, aliseda** — inmuebles bancarios de adjudicación. *Estos son SPAs JavaScript; el scraper estático solo captura el subset SSR-renderizado.*

## Stack

- Python 3.11+ · FastAPI · SQLAlchemy 2 · SQLite
- Anthropic SDK (Claude Opus 4.7) con prompt caching y structured outputs
- httpx + BeautifulSoup para scraping
- Telegram Bot API para las notificaciones
- GitHub Actions para la ejecución diaria en la nube

## Quickstart — todo se ejecuta gratis en GitHub Actions

### 1. Crear el bot de Telegram (1 min)

1. Abre Telegram y busca **@BotFather**.
2. Manda `/newbot`, sigue las instrucciones, copia el **bot token** (`123456:ABC-DEF...`).
3. Abre tu nuevo bot y mándale cualquier mensaje (`/start`).
4. Para encontrar tu **chat_id**: habla con **@userinfobot** y te da tu ID (un número como `987654321`).

### 2. Configurar los secrets en GitHub

En tu repo, ve a **Settings → Secrets and variables → Actions** y añade:

| Tipo | Nombre | Valor |
|---|---|---|
| Secret | `ANTHROPIC_API_KEY` | tu API key de Anthropic |
| Secret | `TELEGRAM_BOT_TOKEN` | el token del bot |
| Secret | `TELEGRAM_CHAT_ID` | tu chat_id |

Opcional, en la pestaña **Variables** del mismo sitio puedes ajustar:

| Nombre | Default | Descripción |
|---|---|---|
| `ANALYSIS_MODEL` | `claude-opus-4-7` | Cambiable a `claude-sonnet-4-6` para abaratar |
| `TARGET_PROVINCES` | `08,17,25,43` | Provincias INE |
| `MAX_PRICE_EUR` | `400000` | Filtra antes del LLM |
| `MIN_OPPORTUNITY_SCORE` | `60` | Score mínimo para enviar |
| `ENABLED_SCRAPERS` | `boe,addmeet,solvia,haya,servihabitat,aliseda` | |

### 3. Listo

El workflow `.github/workflows/daily.yml` corre cada día a las 07:00 UTC (08:00 Madrid invierno / 09:00 verano). Para cambiar la hora edita la línea `cron:` del workflow.

Para probar antes de esperar al siguiente día: ve a **Actions → Daily auction analysis → Run workflow** y dispáralo manualmente.

La base SQLite se cachea entre corridas (vía `actions/cache`) para que no re-analice las mismas subastas cada día.

---

## Uso local (alternativa)

```bash
cd sell_analysis
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edita .env con tu ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID

python run_daily.py            # corrida única
python run_daily.py --dry-run  # todo menos el envío a Telegram
```

### Servidor con API web (opcional)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Esto inicia un scheduler interno y expone una API:

- `GET /health`
- `GET /auctions?source=boe&province=Barcelona&min_score=60&limit=50`
- `GET /auctions/{id}`
- `GET /reports/latest`
- `GET /preview?min_score=60` — vista HTML del reporte del día
- `POST /run` — dispara una corrida en background

## Arquitectura

```
sell_analysis/
├── .github/workflows/daily.yml   # Cron diario en GitHub Actions
├── app/
│   ├── config.py                 # Settings (pydantic-settings)
│   ├── db.py                     # SQLAlchemy engine + session
│   ├── models.py                 # Auction / Analysis / DailyReport
│   ├── scrapers/
│   │   ├── base.py               # BaseScraper + AuctionItem dataclass
│   │   ├── boe.py                # subastas.boe.es (más completo)
│   │   ├── addmeet.py
│   │   ├── _bank_base.py         # base genérica para bancos
│   │   └── solvia.py / haya.py / servihabitat.py / aliseda.py
│   ├── analyzer.py               # Claude con structured outputs + prompt caching
│   ├── telegram.py               # Envío a Telegram Bot API
│   ├── notifier.py               # Render Jinja para /preview
│   ├── pipeline.py               # Orquestador: scrape → upsert → analyze → notify
│   ├── scheduler.py              # APScheduler cron (uso local)
│   ├── main.py                   # FastAPI
│   └── templates/email.html.j2
├── tests/test_boe_parser.py
├── run_daily.py
├── requirements.txt
└── .env.example
```

### Cómo funciona el análisis

`app/analyzer.py` envía cada subasta a Claude con:

- **System prompt cacheado** (`cache_control: ephemeral`): instrucciones constantes → llamadas posteriores cuestan ~10× menos en tokens de input.
- **Adaptive thinking**: Claude decide cuánto razonar por subasta.
- **Structured outputs**: el JSON está validado contra un Pydantic schema (`OpportunityAnalysis`), así que nunca recibes JSON malformado.

Salida (en español):

- `opportunity_score` 0-100
- `estimated_market_value_eur`, `estimated_discount_pct`
- `estimated_rental_yield_pct`, `estimated_flip_roi_pct`
- `summary`, `pros[]`, `cons[]`, `legal_risks[]`
- `recommendation`: `"comprar"` / `"investigar"` / `"descartar"`

## Mantenimiento de scrapers

Las páginas web cambian. Mira los logs:

```bash
python run_daily.py --dry-run 2>&1 | grep -i "scraper\|failed"
```

El scraper de **BOE** es el más estable (HTML del gobierno, estructura tabular).

Los scrapers de **bancos** son SPAs JavaScript y la versión actual solo captura lo SSR. Para cobertura completa, sustituye `httpx.Client` por `playwright` en `_bank_base.py`.

## Tests

```bash
pytest
```

## Coste estimado de Claude

Por subasta (Opus 4.7, system cacheado, ~500 tokens de input variable + ~600 tokens de output):

- Input no cacheado: ~$0.0025
- Output: ~$0.015
- **~$0.018 por subasta analizada**

Con 30-50 subastas/día son ~$0.50-$0.90/día. Bajar a `claude-sonnet-4-6` lo deja en ~$0.15-$0.27/día.

## Notas legales

- Respeta robots.txt y términos de servicio de cada plataforma.
- BOE es información pública.
- Los portales bancarios y privados pueden requerir consentimiento para scraping a escala — limita la frecuencia y usa el `User-Agent` configurable.
