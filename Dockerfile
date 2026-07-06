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

# LOW_MEMORY_MODE: Bothost — жёсткий лимит RAM (был OOM/SIGKILL цикл).
# Урезает AI-ансамбль до 3 моделей (RF+ET+GB, вместо 6) и включает
# malloc_trim(0) после каждого fit(), чтобы освобождённая память реально
# возвращалась ОС, а не оседала в аренах glibc malloc.
ENV LOW_MEMORY_MODE=1
ENV PORT=3000
EXPOSE 3000

# Health check: Bothost nginx начнёт роутить трафик только когда /health отвечает 200
HEALTHCHECK --interval=15s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:3000/health || exit 1

# Gunicorn: 1 воркер + 4 треда — обязательно для Flask-SocketIO (async_mode=threading).
# Тредов меньше, чем на обычном хосте (было 8) — каждый тред Python держит свой
# стек и локальные буферы, на 256-512MB хосте это заметная доля RAM.
# --max-requests: safety-сеть — воркер сам перезапускается после N запросов,
# чтобы сбрасывать любой постепенный рост RAM (не даёт памяти "расползтись" за часы работы)
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "4", "--timeout", "120", \
     "--max-requests", "2000", "--max-requests-jitter", "200", "main:app"]
