APP_DIR  := $(shell pwd)
CONF     := $(APP_DIR)/deploy/supervisord.conf
CTL      := supervisorctl -c $(CONF)

.PHONY: deploy up down status logs logs-scraper logs-dashboard restart \
        scrape-once scrape-rentals show-top export build-docker run-docker

# ── Despliegue ────────────────────────────────────────────────────────────────

deploy:
	@bash $(APP_DIR)/deploy/deploy.sh

up:
	$(CTL) start all

down:
	$(CTL) stop all

restart:
	$(CTL) restart all

status:
	$(CTL) status

# ── Logs ──────────────────────────────────────────────────────────────────────

logs:
	tail -f $(APP_DIR)/data/scraper.log $(APP_DIR)/data/dashboard.log

logs-scraper:
	tail -f $(APP_DIR)/data/scraper.log

logs-dashboard:
	tail -f $(APP_DIR)/data/dashboard.log

# ── Comandos manuales ─────────────────────────────────────────────────────────

scrape-once:
	cd $(APP_DIR) && python3 main.py scrape --once

scrape-rentals:
	cd $(APP_DIR) && python3 main.py scrape-rentals

show-top:
	cd $(APP_DIR) && python3 main.py show-top

export:
	cd $(APP_DIR) && python3 main.py export --output data/export.csv

# ── Docker (si hay daemon disponible) ────────────────────────────────────────

build-docker:
	docker compose build

run-docker:
	docker compose up -d
