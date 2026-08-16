# GRINCH-GRAM .github/ — Полная DevSecOps инфраструктура

## Что было сделано

Полностью переработанная и дополненная директория `.github/` для репозитория GRINCH-GRAM.

### Исправленные проблемы оригинала

| Проблема | Исправление |
|----------|-------------|
| AI Orchestrator ссылался на несуществующие workflow | Созданы `ai-bug-hunter.yml`, `ai-security-audit.yml`, `ai-docs-sync.yml` |
| SARIF upload из Markdown (ai-nightly) | Убран сломанный upload-sarif, заменён на git-auto-commit |
| IP VPS захардкожен в workflow | Вынесен в `secrets.VPS_HOST` + `secrets.VPS_USER` |
| Нет CODEOWNERS | Добавлен с разделением по областям |
| Нет шаблонов Issues/PR | Добавлены bug_report.yml, feature_request.yml, PR template |
| Нет labeler | Добавлен авто-лейблинг по путям |
| Bandit не блокировал CI | Добавлен артефакт + security.yml с SARIF upload |
| Нет авто-мержа Dependabot | Добавлен `dependabot-auto-merge.yml` |
| Нет очистки stale issues | Добавлен `stale.yml` |
| Нет CodeQL | Добавлен `codeql.yml` |
| Нет health-check после деплоя | Добавлен curl /health в `vps-deploy.yml` |
| Нет Docker publish | Добавлен `docker-publish.yml` (GHCR) |
| Нет release automation | Добавлен `release.yml` с changelog |
| Нет OpenSSF Scorecard | Добавлен `scorecard.yml` |
| Нет dependency review | Добавлен `dependency-review.yml` |
| Нет AI self-improvement | Добавлен `ai-self-improve.yml` (weekly refactoring plan) |
| Нет supreme agent | Добавлен `ai-supreme-agent.yml` (weekly strategic analysis) |
| Нет README generation | Добавлен `generate-readme.yml` (AI-generated README) |

## Структура

```
.github/
├── CODEOWNERS                          # Владельцы кода
├── PULL_REQUEST_TEMPLATE.md            # Шаблон PR
├── labeler.yml                         # Авто-лейблинг
├── AUTOMATION.md                       # Документация (из оригинала)
├── dependabot.yml                      # Автообновление зависимостей
├── ISSUE_TEMPLATE/
│   ├── bug_report.yml                  # Форма баг-репорта
│   └── feature_request.yml             # Форма фич-реквеста
└── workflows/
    ├── ci.yml                          # CI/CD + Docker smoke test
    ├── ai-orchestrator.yml             # Оркестратор AI-агентов (ИСПРАВЛЕН)
    ├── ai-code-review.yml              # AI ревью кода
    ├── ai-bug-hunter.yml               # 🆕 Статический анализ + AI
    ├── ai-security-audit.yml           # 🆕 Security scan + AI
    ├── ai-docs-sync.yml                # 🆕 Проверка документации
    ├── ai-nightly-deep-audit.yml       # Ночной аудит (ИСПРАВЛЕН)
    ├── ai-self-improve.yml             # 🆕 Weekly refactoring plan
    ├── ai-supreme-agent.yml            # 🆕 Weekly strategic analysis
    ├── vps-deploy.yml                  # Деплой на VPS (ИСПРАВЛЕН)
    ├── security.yml                    # 🆕 Bandit SARIF + Trivy
    ├── codeql.yml                      # 🆕 GitHub CodeQL
    ├── docker-publish.yml              # 🆕 Docker build & push to GHCR
    ├── release.yml                     # 🆕 Auto-release with changelog
    ├── scorecard.yml                   # 🆕 OpenSSF Scorecard
    ├── dependency-review.yml           # 🆕 PR dependency review
    ├── dependabot-auto-merge.yml       # 🆕 Авто-мерж patch/minor
    ├── generate-readme.yml             # 🆕 AI-generated README
    └── stale.yml                       # 🆕 Авто-закрытие старых issues
```

## Обязательные Secrets

Добавьте в Settings → Secrets and variables → Actions:

| Secret | Описание |
|--------|----------|
| `GROQ_API_KEY` | API ключ для AI-анализа (Groq) |
| `VPS_SSH_KEY` | Приватный SSH-ключ для деплоя |
| `VPS_HOST` | IP или домен VPS (было 2.27.25.126) |
| `VPS_USER` | Пользователь SSH (default: root) |

## Установка

```bash
# 1. Распакуйте архив в корень репозитория
unzip grinch-gram-github-fixed.zip -d /path/to/repo/

# 2. Добавьте Secrets (см. выше)

# 3. Удалите или отредактируйте DEPLOY.md — там открытый IP

# 4. Закоммитьте и запушьте
git add .github/
git commit -m "ci: полностью переработана DevSecOps инфраструктура"
git push origin main
```

## Триггеры workflow

| Workflow | Триггер | Описание |
|----------|---------|----------|
| ci.yml | push, PR, manual | CI/CD + Docker smoke test |
| ai-orchestrator.yml | push, PR, manual | Оркестратор AI-агентов |
| ai-code-review.yml | PR (из оркестратора) | AI ревью кода |
| ai-bug-hunter.yml | PR (из оркестратора) | Статический анализ + AI |
| ai-security-audit.yml | PR, cron 03:00 | Security scan + AI |
| ai-docs-sync.yml | PR (из оркестратора) | Проверка документации |
| ai-nightly-deep-audit.yml | cron 02:00, manual | Ночной аудит |
| ai-self-improve.yml | cron вс 01:00, manual | План рефакторинга |
| ai-supreme-agent.yml | cron вс 00:00, manual | Стратегический анализ |
| vps-deploy.yml | manual, repository_dispatch | Деплой на VPS |
| security.yml | push, PR, cron 04:00 | Bandit + Trivy |
| codeql.yml | push, PR, cron пн 05:00 | GitHub CodeQL |
| docker-publish.yml | push, tag, manual | Docker → GHCR |
| release.yml | tag v* | Auto-release |
| scorecard.yml | push, cron пн 07:00 | OpenSSF Scorecard |
| dependency-review.yml | PR | Review зависимостей |
| dependabot-auto-merge.yml | PR от dependabot | Авто-мерж |
| generate-readme.yml | push, manual | AI README |
| stale.yml | cron 06:00 | Очистка issues |
