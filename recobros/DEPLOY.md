# Desplegar el panel de recobros

El panel es una app Streamlit (`recobros/app.py`). En el primer arranque importa
`alumnos.csv` y genera `data/recobros.db` automáticamente, así que **no hay que
configurar base de datos** para verlo funcionando.

## Opción A — Streamlit Community Cloud (la más rápida, gratis) ✅

Para verlo con una URL compartible en ~2 minutos:

1. Entra en https://share.streamlit.io e inicia sesión con GitHub.
2. **New app** → **Deploy a public app from GitHub**.
3. Rellena:
   - **Repository**: `charleskot/sell_analysis`
   - **Branch**: `claude/collections-tracking-panel-p7ycaz`
   - **Main file path**: `recobros/app.py`
4. **Deploy**. La primera vez instala dependencias (~2-3 min) y arranca.

Obtienes una URL tipo `https://<algo>.streamlit.app` que puedes pasar a la
persona de recobros. La base de datos es efímera (se regenera desde `alumnos.csv`
en cada reinicio) — perfecto para revisar; para uso real, ver Railway.

## Opción B — Railway (infra propia, persistente)

El repo ya trae `Dockerfile` y `railway.json` (que hoy arranca el *scraper*).
Para el panel de recobros, crea un **servicio nuevo** en el mismo proyecto:

1. Railway → tu proyecto → **New** → **GitHub Repo** → `sell_analysis`,
   branch `claude/collections-tracking-panel-p7ycaz`.
2. En **Settings → Deploy → Start Command**:
   ```
   streamlit run recobros/app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
   ```
3. **Settings → Networking → Generate Domain** para obtener la URL pública.
4. (Opcional) Añade un **Volume** montado en `/app/data` para que la base de
   datos y las gestiones persistan entre despliegues.
5. (Opcional) Variables de entorno para las integraciones:
   `HUBSPOT_TOKEN`, `HUBSPOT_PROP_MOROSIDAD`, `WOO_URL`, `WOO_KEY`, `WOO_SECRET`.

## Opción C — Docker en cualquier servidor

```bash
docker build -t recobros .
docker run -p 8501:8501 -v $(pwd)/data:/app/data \
  recobros streamlit run recobros/app.py \
  --server.address 0.0.0.0 --server.port 8501 --server.headless true
```
Panel en `http://<host>:8501`.
