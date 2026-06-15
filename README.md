# D-Link DIR-825 Exporter

Prometheus exporter for D-Link DIR-825 routers. Collects metrics via CPE JSON-RPC API and writes syslog to a file for Promtail to ship to Loki.

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

## Metrics Exported

All metrics use **generic naming** — no vendor-specific prefixes — so they remain consistent when other routers/APs (MikroTik, Ubiquiti, etc.) are added later.

| Metric | Source | Labels |
|---|---|---|
| `device_info` | CPE API | model, vendor, fw_version, hw_revision, mode, mac |
| `device_uptime_seconds` | CPE API | — |
| `wifi_radio_status` | CPE API | band (2.4GHz, 5GHz) |
| `wifi_ap_enabled` | CPE API | ssid, band |
| `interface_up` | CPE API | name, type (wifi/bridge/loopback) |
| `lan_ip_info` | CPE API | address, type |
| `wan_ip_info` | CPE API | address, type |
| `wan_connection_status` | CPE API | status |
| `switch_port_enabled` | CPE API | port, alias |
| `system_time_seconds` | CPE API | — |
| `dhcp_lease_count` | placeholder | — |
| `dhcp_lease_info` | placeholder | hostname, ip, mac |

## Limitations

**`Device.Statistics.*` (detailed port/interface stats) is NOT accessible via the CPE API** on this router model. The Angular web UI loads these stats through a separate internal data layer (`dsysinit`) that isn't exposed to external API calls. This means the following metrics require future web-scraping enhancement:

- Port traffic (bytes/packets sent/received)
- Port errors (CRC, discards, collisions, fragments)
- Port utilization and current speed
- Interface network stats (RX/TX bytes per interface)
- DHCP lease details
- Connected WiFi clients (MAC, IP, hostname)
- Full syslog entries

These will be added in a future release via headless browser or XHR interception.

## Quick Start

```bash
pip install -r requirements.txt

# Create config.yaml from example
cp config.yaml.example config.yaml
# Edit config.yaml with your router credentials

# Run
python dlink_exporter.py
```

## Configuration

### config.yaml

```yaml
router:
  host: "10.0.0.1"
  username: "admin"
  password: "your_password"

exporter:
  listen_address: "0.0.0.0"
  listen_port: 9101
  scrape_interval: 60
  log_scrape_interval: 30

logging:
  log_file: "/var/log/dlink/syslog.log"
  log_max_size: 10485760  # 10MB
```

### Environment variables (override config.yaml)

- `DLINK_CONFIG` — path to config.yaml (default: `config.yaml`)
- `DLINK_ROUTER_HOST` — router IP
- `DLINK_PASSWORD` — router password
- `DLINK_LISTEN_PORT` — exporter port
- `DLINK_LOG_FILE` — syslog output path

## Prometheus Scrape Config

```yaml
scrape_configs:
  - job_name: "dlink-dir825"
    static_configs:
      - targets: ["localhost:9101"]
```

## Promtail Config (for syslog → Loki)

Add to your Promtail config:

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
