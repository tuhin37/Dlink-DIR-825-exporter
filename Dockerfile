# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dlink_exporter.py web_scraper.py VERSION ./
COPY config.yaml.example ./

RUN mkdir -p /var/log/dlink

EXPOSE 9101
ENV DLINK_CONFIG=/app/config.yaml

ENTRYPOINT ["python", "dlink_exporter.py"]
