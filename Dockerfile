FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY grid-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY grid-bot/ .

# Create data dir for SQLite
RUN mkdir -p /app/data

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/status')" || exit 1

EXPOSE 5000

CMD ["python", "app.py"]
