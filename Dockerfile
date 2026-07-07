FROM python:3.11-slim

# Системные зависимости для pytoniq / cryptography / psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

# Сначала только requirements — чтобы docker кэшировал слой зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Остальной код
COPY . .

# Постоянные данные (experience.json, wallets.json, settings.json, backups/)
RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV DATA_DIR=/app/data
ENV LOW_MEMORY_MODE=1
ENV PORT=3000

EXPOSE 3000

CMD gunicorn main:app \
    --worker-class gthread \
    --threads 4 \
    --workers 1 \
    --bind 0.0.0.0:${PORT} \
    --timeout 120 \
    --keep-alive 5 \
    --log-level info
