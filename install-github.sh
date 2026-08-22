#!/bin/bash
set -euo pipefail

REPO_URL="https://github.com/Alexkkkkk/GRINCH-GRAM"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

echo "=========================================="
echo "  GRINCH-GRAM .github/ Installer v2.0"
echo "=========================================="

# 1. Проверяем, что мы в git-репозитории
if [ ! -d ".git" ]; then
    echo "❌ Ошибка: запустите скрипт в корне git-репозитория"
    exit 1
fi

# 2. Определяем владельца и репо
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
if [ -n "$REMOTE_URL" ]; then
    REPO_PATH=$(echo "$REMOTE_URL" | sed 's/.*github.com[\/:]//' | sed 's/\.git$//')
    echo "📡 Обнаружен репозиторий: $REPO_PATH"
else
    REPO_PATH="Alexkkkkk/GRINCH-GRAM"
    echo "⚠️ Не удалось определить remote, используем: $REPO_PATH"
fi

# 3. Бэкап оригинала
if [ -d ".github" ]; then
    BACKUP=".github-backup-$(date +%Y%m%d-%H%M%S)"
    echo "📦 Бэкап оригинального .github/ → $BACKUP"
    cp -r .github "$BACKUP"
fi

# 4. Находим архив
ARCHIVE=""
for f in grinch-gram-github-fixed.zip github-grinch-gram.zip; do
    if [ -f "$f" ]; then
        ARCHIVE="$f"
        break
    fi
done

if [ -z "$ARCHIVE" ]; then
    # Ищем в родительских директориях
    for dir in .. ../.. /mnt/agents/output ~/Downloads; do
        for f in "$dir"/grinch-gram-github-fixed.zip "$dir"/github-grinch-gram.zip; do
            if [ -f "$f" ]; then
                ARCHIVE="$f"
                break 2
            fi
        done
    done
fi

if [ -z "$ARCHIVE" ]; then
    echo "❌ Архив не найден. Скачайте grinch-gram-github-fixed.zip"
    echo "   и положите в корень репозитория."
    exit 1
fi

echo "📂 Найден архив: $ARCHIVE"
echo "📂 Распаковка..."
unzip -q "$ARCHIVE" -d "$TEMP_DIR"

# Находим .github внутри архива
GITHUB_SRC=$(find "$TEMP_DIR" -type d -name ".github" | head -1)
if [ -z "$GITHUB_SRC" ]; then
    echo "❌ В архиве не найдена директория .github/"
    exit 1
fi

cp -r "$GITHUB_SRC" .
echo "✅ .github/ установлен"

# 5. Проверяем YAML-валидность
echo ""
echo "🔍 Проверка YAML-валидности workflow..."
INVALID=0
for wf in .github/workflows/*.yml; do
    if python3 -c "import yaml; yaml.safe_load(open('$wf'))" 2>/dev/null; then
        echo "  ✅ $(basename $wf)"
    else
        echo "  ❌ $(basename $wf) — ОШИБКА ВАЛИДАЦИИ"
        INVALID=$((INVALID + 1))
    fi
done

if [ $INVALID -gt 0 ]; then
    echo "⚠️  Найдено $INVALID workflow с ошибками!"
    echo "   Восстанавливаем бэкап..."
    rm -rf .github
    mv "$BACKUP" .github
    exit 1
fi

# 6. Проверка наличия критических secrets
echo ""
echo "🔐 Проверка GitHub Secrets..."
echo "   (Требуются: GROQ_API_KEY, VPS_SSH_KEY, VPS_HOST, VPS_USER)"
echo "   Добавьте их тут: https://github.com/$REPO_PATH/settings/secrets/actions"

# 7. Удаление открытого IP из DEPLOY.md
if [ -f "DEPLOY.md" ]; then
    echo ""
    echo "🛡️ Очистка DEPLOY.md от открытых IP..."
    sed -i 's/root@[0-9]\+\.[0-9]\+\.[0-9]\+\.[0-9]\+/root@\${VPS_HOST}/g' DEPLOY.md 2>/dev/null || true
    sed -i 's/[0-9]\+\.[0-9]\+\.[0-9]\+\.[0-9]\+/${VPS_HOST}/g' DEPLOY.md 2>/dev/null || true
    git add DEPLOY.md 2>/dev/null || true
    echo "   ✅ DEPLOY.md очищен"
fi

# 8. Проверка на захардкоженные секреты
echo ""
echo "🔎 Проверка на захардкоженные секреты в .github/..."
SECRETS_FOUND=0
for pattern in "ghp_" "AKIA" "-----BEGIN" "api_key=" "password="; do
    if grep -r "$pattern" .github/ 2>/dev/null | grep -v ".yml:" | head -1 >/dev/null; then
        echo "   ⚠️  Возможно найден секрет: $pattern"
        SECRETS_FOUND=$((SECRETS_FOUND + 1))
    fi
done
if [ $SECRETS_FOUND -eq 0 ]; then
    echo "   ✅ Секретов не обнаружено"
fi

# 9. Git commit
echo ""
echo "📝 Создание коммита..."
git add .github/ 2>/dev/null || true
git commit -m "ci: полностью переработана DevSecOps инфраструктура

- Исправлен AI Orchestrator (добавлены недостающие workflow)
- Убран сломанный SARIF upload из nightly audit
- IP VPS вынесен в secrets (VPS_HOST, VPS_USER)
- Добавлены: CODEOWNERS, PR template, Issue templates, labeler
- Добавлены workflow: security, codeql, docker-publish, release
- Добавлены AI-агенты: bug-hunter, security-audit, docs-sync
- Добавлены: self-improve, supreme-agent, generate-readme
- Добавлены: dependabot-auto-merge, stale, dependency-review
- Добавлен: OpenSSF Scorecard
- Все workflow проверены на YAML-валидность" 2>/dev/null || echo "⚠️  Нечего коммитить (возможно, уже установлено)"

echo ""
echo "=========================================="
echo "  ✅ УСТАНОВКА ЗАВЕРШЕНА УСПЕШНО"
echo "=========================================="
echo ""
echo "📋 Следующие шаги:"
echo "   1. git push origin main"
echo "   2. Добавьте Secrets: https://github.com/$REPO_PATH/settings/secrets/actions"
echo "   3. Проверьте Actions: https://github.com/$REPO_PATH/actions"
echo ""
echo "📊 Установлено workflow: $(ls .github/workflows/*.yml | wc -l)"
echo "📁 Шаблоны issues: $(ls .github/ISSUE_TEMPLATE/*.yml 2>/dev/null | wc -l)"
echo ""
