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

# LOW_MEMORY_MODE отключён: подтверждено 4 vCPU / 2GB RAM на контейнере —
# этого достаточно для полного ансамбля из 7 моделей (RF/ET/GB/HGB/XGB/LGB/MLP).
# Если снова начнутся OOM/SIGKILL — включить обратно ENV LOW_MEMORY_MODE=1.
ENV LOW_MEMORY_MODE=0
ENV PORT=3000
EXPOSE 3000

# Health check: Bothost nginx начнёт роутить трафик только когда /health отвечает 200
HEALTHCHECK --interval=15s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:3000/health || exit 1

# Gunicorn: 1 воркер + 8 тредов — обязательно для Flask-SocketIO (async_mode=threading).
# --max-requests: safety-сеть — воркер сам перезапускается после N запросов,
# чтобы сбрасывать любой постепенный рост RAM (не даёт памяти "расползтись" за часы работы)
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "8", "--timeout", "120", \
     "--max-requests", "2000", "--max-requests-jitter", "200", "main:app"]
