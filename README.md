# D-Link DIR-825 Exporter

Prometheus exporter for D-Link DIR-825 routers. Collects metrics via CPE JSON-RPC API and Playwright web scraping. Writes syslog to a file for Promtail to ship to Loki.

Pre-built images: [`fidays/dlink-dir-825-exporter`](https://hub.docker.com/r/fidays/dlink-dir-825-exporter) on Docker Hub (linux/arm64).

## Architecture

```
                    ┌──────────────────────────────┐
Prometheus ────────▶│  dlink_exporter.py           │
                    │  :9101/metrics               │
                    │                              │
                    │  Writes syslog to:           │────▶ /var/log/dlink/syslog.log
                    │                              │             │
                    └──────────────────────────────┘             │
                                                                 ▼
                                                          ┌──────────┐
                                                          │ Promtail │────▶ Loki
                                                          └──────────┘
```

For full web-scraped metrics (port stats, DHCP, WiFi clients), a Playwright/Chromium browser sidecar runs alongside the exporter.

## Metrics Exported

All metrics use **generic naming** — no vendor-specific prefixes — so they remain consistent when other routers/APs (MikroTik, Ubiquiti, etc.) are added later.

### CPE API (always available, 60s interval)

| Metric | Labels |
|---|---|
| `device_info` | model, vendor, fw_version, hw_revision, mode, mac |
| `device_uptime_seconds` | — |
| `wifi_radio_status` | band (2.4GHz, 5GHz) |
| `wifi_ap_enabled` | ssid, band |
| `interface_up` | name, type (wifi/bridge/loopback) |
| `lan_ip_info` | address, type |
| `wan_ip_info` | address, type |
| `wan_connection_status` | status |
| `switch_port_enabled` | port, alias |
| `system_time_seconds` | — |

### Web Scraped (needs Playwright, 5min interval)

| Metric | Labels |
|---|---|
| `interface_rx_bytes`, `interface_tx_bytes` | name |
| `interface_rx_errors`, `interface_tx_errors` | name |
| `port_rx_bytes`, `port_tx_bytes` | port, alias |
| `port_link_up` | port, alias, speed |
| `port_in_utilization_pct`, `port_out_utilization_pct` | port |
| `dhcp_lease_info` | hostname, ip, mac |
| `wifi_connected_clients_total` | — |
| `wifi_client_signal` | mac, hostname, ip, ssid, band |
| `wifi_client_band` | mac, hostname |
| `wifi_client_online` | mac, hostname, ip, ssid |
| `route_info` | destination, gateway, netmask, interface, metric |

---

## Quick Start

### Option 1: Native (bare-metal / VM)

```bash
# Install dependencies
pip install -r requirements.txt

# Create config
cp config.yaml.example config.yaml
# Edit config.yaml with your router password

# Run
python dlink_exporter.py
```

For web-scraped metrics (port stats, DHCP leases, WiFi clients, syslog), install Playwright:

```bash
playwright install chromium
```

### Option 2: Docker Compose (recommended)

```bash
# Clone the repo
git clone https://github.com/tuhin37/Dlink-DIR-825-exporter.git
cd Dlink-DIR-825-exporter

# Create .env with your router credentials
cp config.yaml.example config.yaml
# OR create .env file:
echo "DLINK_ROUTER_HOST=10.0.0.1" >> .env
echo "DLINK_USERNAME=admin" >> .env
echo "DLINK_PASSWORD=your_router_password" >> .env

# Start both exporter and browser sidecar
docker compose up -d
```

This starts two containers:
- **exporter** (`fidays/dlink-dir-825-exporter`) — serves Prometheus metrics on `:9101/metrics`
- **browser-service** (`fidays/dlink-browser-service`) — Playwright/Chromium sidecar for web scraping

### Option 3: Docker (standalone exporter only)

```bash
docker pull fidays/dlink-dir-825-exporter:latest
docker run -d \
  --name dlink-exporter \
  --restart unless-stopped \
  -p 9101:9101 \
  -v /var/log/dlink:/var/log/dlink \
  --env-file .env \
  fidays/dlink-dir-825-exporter:latest
```

> **Note:** The exporter image does NOT include Chromium/Playwright — web-scraped metrics (port stats, DHCP leases, WiFi clients) will show placeholder values unless the browser sidecar is running.

---

## Docker Compose (full setup with browser sidecar)

```yaml
services:
  exporter:
    image: fidays/dlink-dir-825-exporter:latest
    container_name: dlink-exporter
    restart: unless-stopped
    ports:
      - "9101:9101"
    env_file:
      - .env
    environment:
      - DLINK_BROWSER_SERVICE=http://browser-service:9200
    volumes:
      - ./logs:/var/log/dlink
    depends_on:
      browser-service:
        condition: service_healthy

  browser-service:
    image: fidays/dlink-browser-service:latest
    container_name: dlink-browser
    restart: unless-stopped
    ports:
      - "9201:9200"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9200/health"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s
```

## Configuration

Configuration is resolved in this order (later overrides earlier):

1. Defaults (hardcoded in `dlink_exporter.py`)
2. `config.yaml` (if it exists)
3. `.env` file (via `python-dotenv`)
4. Environment variables (highest priority)

### config.yaml

```yaml
router:
  host: "10.0.0.1"
  username: "admin"
  password: "your_password"

exporter:
  listen_address: "0.0.0.0"
  listen_port: 9101       # Metrics port — Prometheus scrapes :9101/metrics
  scrape_interval: 60      # seconds between metric scrapes
  log_scrape_interval: 30  # seconds between syslog scrapes

logging:
  log_file: "/var/log/dlink/syslog.log"
  log_max_size: 10485760   # 10MB
```

### .env file

```bash
# Router connection
DLINK_ROUTER_HOST=10.0.0.1
DLINK_USERNAME=admin
DLINK_PASSWORD=your_router_password_here

# Metrics port (Prometheus scrapes this endpoint)
DLINK_LISTEN_PORT=9101
DLINK_LOG_FILE=/var/log/dlink/syslog.log
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `DLINK_CONFIG` | `config.yaml` | Path to config YAML |
| `DLINK_ROUTER_HOST` | `10.0.0.1` | Router IP address |
| `DLINK_USERNAME` | `admin` | Router admin username |
| `DLINK_PASSWORD` | — | Router admin password (**required**) |
| `DLINK_LISTEN_PORT` | `9101` | Metrics port for Prometheus scraping |
| `DLINK_LOG_FILE` | `/var/log/dlink/syslog.log` | Syslog output path |

## Building from Source

```bash
# Clone the repo
git clone https://github.com/tuhin37/Dlink-DIR-825-exporter.git
cd Dlink-DIR-825-exporter

# Build exporter image
docker build -t fidays/dlink-dir-825-exporter:latest .

# Build browser sidecar image
docker build -t fidays/dlink-browser-service:latest -f docker/browser-service/Dockerfile docker/browser-service/

# Push to Docker Hub
docker push fidays/dlink-dir-825-exporter:latest
docker push fidays/dlink-browser-service:latest

# Tag with version
docker tag fidays/dlink-dir-825-exporter:latest fidays/dlink-dir-825-exporter:v0.1.2
docker tag fidays/dlink-browser-service:latest fidays/dlink-browser-service:v0.1.2
docker push fidays/dlink-dir-825-exporter:v0.1.2
docker push fidays/dlink-browser-service:v0.1.2
```

> **Note:** Update the `VERSION` file before building a new release. Images must be pushed to Docker Hub under the [`fidays`](https://hub.docker.com/u/fidays) namespace.

## Versioning

This project follows [semantic versioning](https://semver.org/) via the `VERSION` file (`vMAJOR.MINOR.PATCH`). Update it manually before tagging a release.

## Prometheus Scrape Config

```yaml
scrape_configs:
  - job_name: "dlink-dir825"
    static_configs:
      - targets: ["localhost:9101"]
```

## Promtail Config (for syslog → Loki)

```yaml
scrape_configs:
  - job_name: dlink-syslog
    static_configs:
      - targets: [localhost]
        labels:
          job: dlink-syslog
          host: dlink-dir825
          __path__: /var/log/dlink/syslog.log
```

## CPE API Reference

Login:
```json
POST /cpe
{"jsonrpc":"2.0","method":"Login","params":{"Login":"admin","Password":"...","StaySigned":true},"id":1}
```

Get parameters:
```json
POST /cpe
{"jsonrpc":"2.0","method":"GetParameterValues","params":{"ParameterNames":["Device.DeviceInfo.Uptime"]},"id":1}
```

Discover tree:
```json
POST /cpe
{"jsonrpc":"2.0","method":"GetParameterNames","params":{"ParameterPath":"Device.","NextLevel":true},"id":1}
```
