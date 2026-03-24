#!/usr/bin/env bash
# deploy.sh — Arranca los servicios via supervisord
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$APP_DIR/deploy/supervisord.conf"
PIDFILE="/tmp/supervisord.pid"

echo "==> App dir: $APP_DIR"

# 1. Inicializar base de datos
echo "==> Inicializando base de datos..."
cd "$APP_DIR"
python3 main.py init-db-cmd

# 2. Cargar .env si existe
if [ -f "$APP_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$APP_DIR/.env"
    set +a
fi

# 3. Si supervisord ya está corriendo, recargar config; si no, arrancarlo
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "==> Supervisord ya está corriendo — recargando configuración..."
    supervisorctl -c "$CONF" reread
    supervisorctl -c "$CONF" update
    supervisorctl -c "$CONF" restart all
else
    echo "==> Arrancando supervisord..."
    supervisord -c "$CONF"
    sleep 2
fi

echo ""
supervisorctl -c "$CONF" status
echo ""
echo "==> Deploy completado."
echo "  Dashboard:     http://localhost:8501"
echo "  Estado:        supervisorctl -c $CONF status"
echo "  Logs scraper:  tail -f $APP_DIR/data/scraper.log"
echo "  Logs dash:     tail -f $APP_DIR/data/dashboard.log"
echo ""
