FROM python:3.11-slim

# Bothost ВАЖНО: /app монтируется с хоста при деплое (bind mount),
# поэтому WORKDIR должен быть /usr/src/app — иначе наш код перезапишется.
WORKDIR /usr/src/app

# Системные зависимости (нужны для cryptography, numpy, pandas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /app/data — персистентная папка Bothost (сохраняется между деплоями)
RUN mkdir -p /app/data

ENV PORT=3000
EXPOSE 3000

# Gunicorn: 1 воркер + 8 тредов — обязательно для Flask-SocketIO (async_mode=threading)
# Порт 3000 — ожидаемый Bothost
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "8", "--timeout", "120", "main:app"]
