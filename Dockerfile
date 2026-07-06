FROM python:3.11-slim

# Bothost ВАЖНО: /app монтируется с хоста при деплое (bind mount),
# поэтому WORKDIR должен быть /usr/src/app — иначе наш код перезапишется.
WORKDIR /usr/src/app

# Системные зависимости (нужны для cryptography, numpy, pandas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
    main:app
