# Production automation

Этот репозиторий содержит торговую и блокчейн-логику, поэтому автоматизация
работает по принципу **fail closed**:

- `CI` публикует обязательные проверки `lint` и `docker-build`.
- `docker-build` не только собирает образ, но и проверяет `/health` в запущенном
  контейнере.
- `Security` публикует `bandit`, `secret-scan`, `dependency-audit` и
  `container-scan`.
- CodeQL и OpenSSF Scorecard выполняются отдельно и загружают результаты в
  GitHub Security.
- Ruff-автофикс и AI-документация создают только pull request. Они никогда не
  коммитят напрямую в `main`.
- Dependabot может включить squash auto-merge только для patch/minor-обновлений
  после успешных обязательных проверок.
- Ночные проверки повторяют аудит зависимостей и контейнера независимо от
  обычного CI.

## Правило для production-кода

Любое изменение `trader.py`, `grid_trader.py`, `dedust_client.py`, wallet-кода,
конфигурации или workflow требует обычного ревью. Автоматические исправления
считаются предложением, а не разрешением на деплой.

## Required checks

В ruleset для `main` должны использоваться точные имена check-run:

```text
lint
docker-build
bandit
secret-scan
dependency-audit
container-scan
```