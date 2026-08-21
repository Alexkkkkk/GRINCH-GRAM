.PHONY: deploy rollback restart logs status backup setup ssh

VPS_HOST ?= 2.27.25.126
VPS_USER ?= deployer

deploy:
	@echo "Push to main branch to trigger auto-deploy via GitHub Actions"
	@git push origin main

rollback:
	@gh workflow run vps-deploy.yml -f action=rollback

restart:
	@gh workflow run vps-deploy.yml -f action=restart

logs:
	ssh $(VPS_USER)@$(VPS_HOST) "docker logs -f grinch-bot --tail 100"

status:
	ssh $(VPS_USER)@$(VPS_HOST) "docker compose -f /opt/bot/docker-compose.prod.yml ps"

backup:
	ssh $(VPS_USER)@$(VPS_HOST) "bash /opt/bot/scripts/backup.sh"

setup:
	ssh root@$(VPS_HOST) "bash -s" < scripts/setup-vps.sh

ssh:
	ssh $(VPS_USER)@$(VPS_HOST)
