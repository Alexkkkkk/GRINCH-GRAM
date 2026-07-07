FROM python:3.11-slim

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ⛔  BOTHOST-LOCKED SETTINGS — НЕ МЕНЯТЬ БЕЗ ЯВНОГО РАЗРЕШЕНИЯ        ║
# ║                                                                          ║
# ║  Все параметры ниже выверены по docs.bothost.ru и РАБОТАЮТ на продакшне.║
# ║  Изменение любого из них ломает деплой на Bothost.                       ║
# ║                                                                          ║
# ║  WORKDIR=/usr/src/app  — НЕ /app  (Bothost bind-монтирует /app с Git,  ║
# ║                                    артефакты сборки в /app исчезают)    ║
# ║  DATA_DIR=/app/data    — персистентный том Bothost (переживает деплой)  ║
# ║  PORT=3000             — читается через ${PORT:-3000}, НЕ хардкод       ║
# ║  CMD: shell-форма      — для раскрытия $PORT                            ║
# ║  --worker-class gthread— обязателен для Flask-SocketIO threading-режима ║
# ║  LOW_MEMORY_MODE=1     — Bothost лимит ~255MB RAM, без этого OOM        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Bothost ВАЖНО: /app монтируется с хоста при деплое (bind mount),
# поэтому WORKDIR должен быть /usr/src/app — иначе наш код перезапишется.
WORKDIR /usr/src/app

# Только curl — нужен для HEALTHCHECK.
# gcc/g++ НЕ нужны: все пакеты (numpy, pandas, cryptography, xgboost,
# psycopg2-binary и т.д.) поставляются готовыми binary wheels для
# Python 3.11 / Linux x86_64 и не требуют компиляции.
# Удаление компилятора экономит ~400 МБ места на диске при сборке.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

# requirements-bothost.txt = requirements.txt без xgboost (131.7 МБ),
# ccxt (5.8 МБ) и eventlet (0.4 МБ) — они переполняли диск при сборке.
# xgboost при LOW_MEMORY_MODE=1 всё равно не импортируется (ai_engine.py:63).
# ccxt импортируется лениво внутри try/except в exchange.py — без него бот
# просто остаётся в DeDust-режиме. eventlet не используется совсем.
COPY requirements-bothost.txt .
# --prefer-binary: pip выберет готовое колесо вместо сборки из исходников.
RUN pip install --no-cache-dir --prefer-binary -r requirements-bothost.txt

COPY . .

# /app/data — персистентная папка Bothost (сохраняется между деплоями)
RUN mkdir -p /app/data

# ВРЕМЕННЫЙ ОТКАТ (2026-07-06): после включения полного ансамбля контейнер
# на Bothost начал получать повторные SIGTERM от платформы и зависать —
# несмотря на заявленные 4 vCPU/2GB, похоже реальная квота на этом тарифе
# жёстче. Возвращаем экономный режим (3 модели) до выяснения с поддержкой
# Bothost реального лимита памяти контейнера.
ENV LOW_MEMORY_MODE=1
ENV PORT=3000
# Bothost: /app/data — персистентная директория (сохраняется между деплоями)
# settings.json, session_secret и прочие рабочие файлы пишутся сюда
ENV DATA_DIR=/app/data
EXPOSE 3000

# Health check: Bothost nginx начнёт роутить трафик только когда /health отвечает 200
HEALTHCHECK --interval=15s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT:-3000}/health || exit 1

# Gunicorn: 1 воркер + 4 треда — обязательно для Flask-SocketIO (async_mode=threading).
# --worker-class gthread ОБЯЗАТЕЛЕН для Flask-SocketIO в threading-режиме.
# Shell-форма CMD нужна чтобы $PORT раскрывался из env (Bothost передаёт порт через PORT).
# --max-requests: воркер сам перезапускается после N запросов → сброс постепенного роста RAM.
CMD gunicorn --bind 0.0.0.0:${PORT:-3000} \
    --workers 1 \
    --worker-class gthread \
    --threads 4 \
    --timeout 120 \
    --max-requests 2000 \
    --max-requests-jitter 200 \
    --capture-output \
    --log-level debug \
    --access-logfile - \
    --error-logfile - \
    main:app
