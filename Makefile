# ==============================================================================
# Anonymous AUA Telegram Bot - Server Automation Makefile
# ==============================================================================

SHELL := /bin/bash
PROJECT_DIR := $(shell pwd)
SERVICE_NAME := aua-bot
COMPOSE := docker compose

.PHONY: help init start up run stop down restart logs logs-bot logs-db ps status autostart enable-boot update clean db-shell backup restore

# Default target: display help
all: help

help:
	@echo "================================================================================"
	@echo "🎭 Anonymous AUA Telegram Bot — Server Management Commands 🥷"
	@echo "================================================================================"
	@echo "  make start (or run)    Build and start containers in the background"
	@echo "  make autostart         Configure Linux systemd so bot starts automatically on boot"
	@echo "  make stop (or down)    Stop and remove active containers"
	@echo "  make restart           Restart the bot and database containers"
	@echo "  make logs              Stream logs for all services"
	@echo "  make logs-bot          Stream logs for the bot container only"
	@echo "  make logs-db           Stream logs for PostgreSQL only"
	@echo "  make status (or ps)    Display current status and health of containers"
	@echo "  make update            Pull latest git changes and rebuild containers"
	@echo "  make db-shell          Open an interactive PostgreSQL psql session"
	@echo "  make backup            Trigger an instant database backup snapshot"
	@echo "  make restore           Restore database from backups/latest.sql.gz"
	@echo "  make clean             Clean up unused Docker images and build caches"
	@echo "================================================================================"

# Initialize .env file if it does not exist
init:
	@if [ ! -f .env ]; then \
		echo "⚙️ Creating .env from .env.example..."; \
		cp .env.example .env; \
		echo "⚠️ Please edit .env with your BOT_TOKEN and ADMIN_IDS before starting!"; \
	else \
		echo "✓ .env file already exists."; \
	fi

# Start the bot and database in background
start: init
	@echo "🚀 Starting Anonymous AUA Bot with automatic restart..."
	@which systemctl >/dev/null 2>&1 && sudo systemctl enable docker 2>/dev/null || true
	$(COMPOSE) up -d --build
	@echo ""
	@echo "✅ Containers are running! View live logs using: make logs-bot"

run: start
up: start

# Setup systemd autostart so containers launch on OS boot/restart
autostart:
	@echo "🔧 Configuring system-level autostart on boot..."
	@if which systemctl >/dev/null 2>&1; then \
		echo "Enabling Docker daemon autostart..."; \
		sudo systemctl enable docker || true; \
		echo "Creating systemd unit file /etc/systemd/system/$(SERVICE_NAME).service..."; \
		echo "[Unit]" | sudo tee /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "Description=Anonymous AUA Telegram Bot Service" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "Requires=docker.service" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "After=docker.service network-online.target" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "Wants=network-online.target" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "[Service]" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "Type=oneshot" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "RemainAfterExit=yes" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "WorkingDirectory=$(PROJECT_DIR)" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "ExecStart=/usr/bin/docker compose up -d" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "ExecStop=/usr/bin/docker compose down" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "TimeoutStartSec=0" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "[Install]" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		echo "WantedBy=multi-user.target" | sudo tee -a /etc/systemd/system/$(SERVICE_NAME).service > /dev/null; \
		sudo systemctl daemon-reload; \
		sudo systemctl enable $(SERVICE_NAME).service; \
		echo "✅ Systemd service installed and enabled!"; \
		echo "The bot will now ALWAYS start automatically whenever the computer or server boots up."; \
	else \
		echo "ℹ️ Systemd not detected in this environment. Docker's 'restart: always' policy will handle container autostart."; \
	fi

enable-boot: autostart

# Stop services
stop:
	@echo "🛑 Stopping Anonymous AUA services..."
	$(COMPOSE) down

down: stop

# Restart services
restart:
	@echo "🔄 Restarting services..."
	$(COMPOSE) restart
	@echo "✓ Restart complete."

# View logs
logs:
	$(COMPOSE) logs -f

logs-bot:
	$(COMPOSE) logs -f bot

logs-db:
	$(COMPOSE) logs -f postgres

# Container status
status:
	$(COMPOSE) ps

ps: status

# Pull latest git changes and restart
update:
	@echo "📥 Pulling latest git updates..."
	git pull
	@echo "🔄 Rebuilding and restarting containers..."
	$(COMPOSE) up -d --build
	@echo "✓ Update complete and bot is running!"

# Open PostgreSQL shell
db-shell:
	@echo "🔑 Connecting to PostgreSQL database (aua_chat_db)..."
	docker exec -it aua_postgres psql -U $${POSTGRES_USER:-bruce} -d $${POSTGRES_DB:-aua_chat_db}

# Trigger manual database backup
backup:
	@echo "💾 Triggering database backup snapshot..."
	@docker exec aua_bot python -c "import asyncio; from app.services.backup import create_database_backup; ok, name, sz = asyncio.run(create_database_backup()); print(f'Backup status: ok={ok}, file={name}, size={sz} bytes')"

# Restore database from latest backup
restore:
	@if [ ! -f backups/latest.sql.gz ]; then \
		echo "❌ backups/latest.sql.gz does not exist!"; \
		exit 1; \
	fi
	@echo "⚠️ Restoring database from backups/latest.sql.gz..."
	gunzip -c backups/latest.sql.gz | docker exec -i aua_postgres psql -U $${POSTGRES_USER:-bruce} -d $${POSTGRES_DB:-aua_chat_db}
	@echo "✅ Database restored successfully!"

# Clean unused docker resources
clean:
	@echo "🧹 Cleaning unused Docker images and build caches..."
	docker image prune -f
	@echo "✓ Clean complete."

