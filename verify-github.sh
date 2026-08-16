#!/bin/bash
set -euo pipefail

echo "=========================================="
echo "  GRINCH-GRAM .github/ Verifier v2.0"
echo "=========================================="

ERRORS=0
WARNINGS=0

# 1. Проверка структуры
echo ""
echo "📁 Проверка структуры..."
REQUIRED_FILES=(
    ".github/CODEOWNERS"
    ".github/PULL_REQUEST_TEMPLATE.md"
    ".github/labeler.yml"
    ".github/dependabot.yml"
    ".github/ISSUE_TEMPLATE/bug_report.yml"
    ".github/ISSUE_TEMPLATE/feature_request.yml"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [ -f "$file" ]; then
        echo "  ✅ $file"
    else
        echo "  ❌ $file — ОТСУТСТВУЕТ"
        ERRORS=$((ERRORS + 1))
    fi
done

# 2. Проверка workflow
echo ""
echo "🔧 Проверка workflow файлов..."
REQUIRED_WORKFLOWS=(
    "ci.yml"
    "ai-orchestrator.yml"
    "ai-code-review.yml"
    "ai-bug-hunter.yml"
    "ai-security-audit.yml"
    "ai-docs-sync.yml"
    "ai-nightly-deep-audit.yml"
    "ai-self-improve.yml"
    "ai-supreme-agent.yml"
    "vps-deploy.yml"
    "security.yml"
    "codeql.yml"
    "docker-publish.yml"
    "release.yml"
    "scorecard.yml"
    "dependency-review.yml"
    "dependabot-auto-merge.yml"
    "generate-readme.yml"
    "stale.yml"
)

for wf in "${REQUIRED_WORKFLOWS[@]}"; do
    path=".github/workflows/$wf"
    if [ -f "$path" ]; then
        echo "  ✅ $wf"
    else
        echo "  ❌ $wf — ОТСУТСТВУЕТ"
        ERRORS=$((ERRORS + 1))
    fi
done

# 3. YAML валидация
echo ""
echo "🔍 YAML-валидация..."
for wf in .github/workflows/*.yml; do
    name=$(basename "$wf")
    if python3 -c "import yaml; yaml.safe_load(open('$wf'))" 2>/dev/null; then
        echo "  ✅ $name — валиден"
    else
        echo "  ❌ $name — ОШИБКА YAML"
        ERRORS=$((ERRORS + 1))
    fi
done

# 4. Проверка на хардкод IP
echo ""
echo "🛡️ Проверка на захардкоженные IP..."
IP_PATTERN='[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+'
if grep -rE "$IP_PATTERN" .github/workflows/ 2>/dev/null | grep -v "VPS_HOST" | grep -v "secrets\." | head -5; then
    echo "  ⚠️  Найдены захардкоженные IP! Используйте secrets.VPS_HOST"
    WARNINGS=$((WARNINGS + 1))
else
    echo "  ✅ IP не захардкожены"
fi

# 5. Проверка на захардкоженные токены
echo ""
echo "🔐 Проверка на токены/ключи..."
TOKEN_PATTERNS=("ghp_" "github_pat_" "AKIA" "sk-" "-----BEGIN PRIVATE KEY")
for pattern in "${TOKEN_PATTERNS[@]}"; do
    if grep -r "$pattern" .github/ 2>/dev/null | grep -v ".pyc" | head -1 >/dev/null; then
        echo "  ⚠️  Возможно найден токен: $pattern"
        WARNINGS=$((WARNINGS + 1))
    fi
done
if [ $WARNINGS -eq 0 ]; then
    echo "  ✅ Токенов не обнаружено"
fi

# 6. Проверка CODEOWNERS
echo ""
echo "👥 Проверка CODEOWNERS..."
if grep -q "@Alexkkkkk" .github/CODEOWNERS 2>/dev/null; then
    echo "  ✅ CODEOWNERS настроен"
else
    echo "  ⚠️  CODEOWNERS может быть неполным"
    WARNINGS=$((WARNINGS + 1))
fi

# 7. Проверка dependabot
echo ""
echo "📦 Проверка Dependabot..."
if grep -q "package-ecosystem" .github/dependabot.yml 2>/dev/null; then
    echo "  ✅ Dependabot настроен"
else
    echo "  ⚠️  Dependabot может быть не настроен"
    WARNINGS=$((WARNINGS + 1))
fi

# 8. Итог
echo ""
echo "=========================================="
if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo "  ✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ"
    echo "=========================================="
    exit 0
elif [ $ERRORS -eq 0 ]; then
    echo "  ⚠️  ПРОВЕРКИ ПРОЙДЕНЫ С ПРЕДУПРЕЖДЕНИЯМИ ($WARNINGS)"
    echo "=========================================="
    exit 0
else
    echo "  ❌ НАЙДЕНО ОШИБОК: $ERRORS, ПРЕДУПРЕЖДЕНИЙ: $WARNINGS"
    echo "=========================================="
    exit 1
fi
