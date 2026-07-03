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

ENV PORT=3000
EXPOSE 3000

# Health check: Bothost nginx начнёт роутить трафик только когда /health отвечает 200
HEALTHCHECK --interval=15s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:3000/health || exit 1

# Gunicorn: 1 воркер + 8 тредов — обязательно для Flask-SocketIO (async_mode=threading)
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "8", "--timeout", "120", "main:app"]
