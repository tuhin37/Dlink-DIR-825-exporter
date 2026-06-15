# syntax=docker/dockerfile:1
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright and headless Chromium
RUN pip install --no-cache-dir playwright>=1.49.0 && \
    playwright install --with-deps chromium && \
    playwright install --with-deps chromium-headless-shell && \
    rm -rf /root/.cache/pip/*

WORKDIR /app
COPY browser_server.py .

EXPOSE 9200

ENTRYPOINT ["python", "browser_server.py"]
