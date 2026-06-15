# syntax=docker/dockerfile:1
FROM python:3.11-slim as base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Production image ---
FROM base as production

WORKDIR /app
COPY dlink_exporter.py web_scraper.py VERSION ./
COPY config.yaml.example ./

# Create log directory
RUN mkdir -p /var/log/dlink

EXPOSE 9101
ENV DLINK_CONFIG=/app/config.yaml

ENTRYPOINT ["python", "dlink_exporter.py"]
