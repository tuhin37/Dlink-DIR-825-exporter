#!/usr/bin/env python3
"""
D-Link DIR-825 Prometheus Exporter

Exports router metrics from CPE JSON-RPC API and writes syslog
to a file for Promtail to ship to Loki.

Metrics use generic naming (interface_, port_, wifi_, dhcp_, route_)
so they remain consistent when other routers/APs are added later.

Config: config.yaml or environment variables
"""

import os
import sys
import time
import json
import logging
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError

from web_scraper import DlinkScraper, ScrapeResult, InterfaceStats, PortStats, DhcpLease, ClientInfo, RouteEntry

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "router": {
        "host": "10.0.0.1",
        "username": "admin",
        "password": "",
    },
    "exporter": {
        "listen_address": "0.0.0.0",
        "listen_port": 9101,
        "scrape_interval": 60,
        "log_scrape_interval": 30,
    },
    "logging": {
        "log_file": "/var/log/dlink/syslog.log",
        "log_max_size": 10 * 1024 * 1024,  # 10MB
    },
}


def load_config():
    """Load config from config.yaml + .env + env vars."""
    # Load .env file first (overrides YAML defaults)
    load_dotenv()

    cfg_path = os.environ.get("DLINK_CONFIG", "config.yaml")
    cfg = dict(DEFAULT_CONFIG)

    # Load YAML if it exists
    p = Path(cfg_path)
    if p.exists():
        with open(p) as f:
            user_cfg = yaml.safe_load(f) or {}
            # Deep merge
            _deep_merge(cfg, user_cfg)

    # Env overrides
    if os.environ.get("DLINK_ROUTER_HOST"):
        cfg["router"]["host"] = os.environ["DLINK_ROUTER_HOST"]
    if os.environ.get("DLINK_USERNAME"):
        cfg["router"]["username"] = os.environ["DLINK_USERNAME"]
    if os.environ.get("DLINK_PASSWORD"):
        cfg["router"]["password"] = os.environ["DLINK_PASSWORD"]
    if os.environ.get("DLINK_LISTEN_PORT"):
        cfg["exporter"]["listen_port"] = int(os.environ["DLINK_LISTEN_PORT"])
    if os.environ.get("DLINK_LOG_FILE"):
        cfg["logging"]["log_file"] = os.environ["DLINK_LOG_FILE"]

    return cfg


def _deep_merge(base, overlay):
    """Recursively merge overlay into base dict."""
    for key, value in overlay.items():
        if isinstance(value, dict) and key in base and isinstance(base[key], dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ---------------------------------------------------------------------------
# CPE JSON-RPC Client
# ---------------------------------------------------------------------------

class CpeClient:
    """Client for the D-Link CPE JSON-RPC API at /cpe."""

    def __init__(self, host, username, password):
        self.base_url = f"http://{host}/cpe"
        self.username = username
        self.password = password
        self.access_token = None
        self.refresh_token = None
        self._last_login = 0

    def _rpc(self, method, params):
        """Make a JSON-RPC call to the CPE endpoint."""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }).encode()
        req = Request(self.base_url, data=payload,
                       headers={"Content-Type": "application/json"},
                       method="POST")
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except URLError as e:
            log.error("CPE request failed: %s", e)
            return {"error": {"message": str(e)}}
        except json.JSONDecodeError as e:
            log.error("CPE response parse failed: %s", e)
            return {"error": {"message": str(e)}}

    def login(self):
        """Authenticate and get AccessToken."""
        result = self._rpc("Login", {
            "Login": self.username,
            "Password": self.password,
            "StaySigned": True,
        })
        if "result" in result:
            self.access_token = result["result"]["AccessToken"]
            self.refresh_token = result["result"].get("RefreshToken", "")
            self._last_login = time.time()
            log.info("Login successful, token: %s...", self.access_token[:8])
            return True
        else:
            log.error("Login failed: %s", result.get("error", {}).get("message", "unknown"))
            return False

    def is_logged_in(self):
        """Check if session is still valid (AccessTimeout=300s)."""
        return self.access_token is not None and (time.time() - self._last_login) < 250

    def ensure_login(self):
        """Re-login if needed."""
        if not self.is_logged_in():
            return self.login()
        return True

    def get_values(self, param_names):
        """Get parameter values from the CPE tree."""
        if not self.ensure_login():
            return []
        result = self._rpc("GetParameterValues", {"ParameterNames": param_names})
        if "result" in result:
            return result["result"].get("ParameterList", [])
        return []

    def get_names(self, path, next_level=False):
        """Discover available parameters under a path."""
        result = self._rpc("GetParameterNames", {
            "ParameterPath": path,
            "NextLevel": next_level,
        })
        if "result" in result:
            return result["result"].get("ParameterList", [])
        return []


# ---------------------------------------------------------------------------
# Metrics Collector
# ---------------------------------------------------------------------------

class MetricsCollector:
    """Scrapes router data and builds Prometheus metric text."""

    def __init__(self, client: CpeClient):
        self.client = client
        self._metrics = ""
        self._last_scrape = 0

    def scrape(self, scraped: Optional[ScrapeResult] = None):
        """Collect all metrics from the router."""
        if not self.client.ensure_login():
            self._metrics = "# ERROR: Cannot authenticate with router\n"
            return

        lines = []
        lines.append("# HELP router_scrape_duration_seconds Time taken for last scrape")
        lines.append("# TYPE router_scrape_duration_seconds gauge")
        t0 = time.time()

        try:
            self._collect_device_info(lines)
            self._collect_wifi(lines)
            self._collect_interfaces(lines)
            self._collect_ips(lines)
            self._collect_switch_ports(lines)
            self._collect_wan(lines)
            self._collect_system_time(lines)
            self._collect_dhcp_leases(lines)
            if scraped:
                self.add_web_scraped_metrics(lines, scraped)
        except Exception as e:
            log.error("Scrape error: %s", e)
            lines.append(f'# ERROR: {e}')

        duration = time.time() - t0
        lines.append(f"router_scrape_duration_seconds {duration:.4f}")
        lines.append(f"router_scrape_timestamp_seconds {time.time()}")

        self._metrics = "\n".join(lines) + "\n"
        self._last_scrape = time.time()

    def get_metrics(self):
        return self._metrics

    # -- Individual collectors ------------------------------------------------

    def _collect_device_info(self, lines):
        params = self.client.get_values([
            "Device.DeviceInfo.Uptime",
            "Device.DeviceInfo.ModelName",
            "Device.DeviceInfo.Vendor",
            "Device.DeviceInfo.Version",
            "Device.DeviceInfo.HardwareRevision",
            "Device.DeviceInfo.DeviceMode",
            "Device.DeviceInfo.FactoryMACAddress",
        ])
        info = {p["Name"]: p["Value"] for p in params}

        lines.append("# HELP device_info Router metadata")
        lines.append("# TYPE device_info gauge")
        labels = (
            f'model="{info.get("Device.DeviceInfo.ModelName","")}",'
            f'vendor="{info.get("Device.DeviceInfo.Vendor","")}",'
            f'fw_version="{info.get("Device.DeviceInfo.Version","")}",'
            f'hw_revision="{info.get("Device.DeviceInfo.HardwareRevision","")}",'
            f'mode="{info.get("Device.DeviceInfo.DeviceMode","")}",'
            f'mac="{info.get("Device.DeviceInfo.FactoryMACAddress","")}"'
        )
        lines.append(f"device_info{{{labels}}} 1")

        uptime_str = info.get("Device.DeviceInfo.Uptime", "0")
        try:
            uptime = int(uptime_str)
        except ValueError:
            uptime = 0
        lines.append("# HELP device_uptime_seconds Router uptime in seconds")
        lines.append("# TYPE device_uptime_seconds counter")
        lines.append(f"device_uptime_seconds {uptime}")

    def _collect_wifi(self, lines):
        params = self.client.get_values([
            "Device.WiFi.Radio.1.OperatingFrequencyBand",
            "Device.WiFi.Radio.1.Status",
            "Device.WiFi.Radio.2.OperatingFrequencyBand",
            "Device.WiFi.Radio.2.Status",
            "Device.WiFi.Radio.1.AccessPoint.1.Enable",
            "Device.WiFi.Radio.2.AccessPoint.1.Enable",
            "Device.WiFi.APProfile.1.SSID",
            "Device.WiFi.APProfile.2.SSID",
        ])
        info = {p["Name"]: p["Value"] for p in params}

        lines.append("# HELP wifi_radio_status WiFi radio enabled (1) or disabled (0)")
        lines.append("# TYPE wifi_radio_status gauge")
        for radio in ("1", "2"):
            band = info.get(f"Device.WiFi.Radio.{radio}.OperatingFrequencyBand", "unknown")
            status = 1 if info.get(f"Device.WiFi.Radio.{radio}.Status", "") == "Enabled" else 0
            lines.append(f'wifi_radio_status{{band="{band}"}} {status}')

        lines.append("# HELP wifi_ap_enabled WiFi access point enabled (1) or disabled (0)")
        lines.append("# TYPE wifi_ap_enabled gauge")
        for radio, band in [("1", "2.4GHz"), ("2", "5GHz")]:
            ap_enabled = info.get(f"Device.WiFi.Radio.{radio}.AccessPoint.1.Enable", "false")
            ssid = info.get(f"Device.WiFi.APProfile.{radio}.SSID", "unknown")
            val = 1 if str(ap_enabled).lower() == "true" else 0
            lines.append(f'wifi_ap_enabled{{ssid="{ssid}",band="{band}"}} {val}')

    def _collect_interfaces(self, lines):
        params = self.client.get_values([
            "Device.Network.Interface.WiFi.1.Name",
            "Device.Network.Interface.WiFi.1.Status",
            "Device.Network.Interface.WiFi.2.Name",
            "Device.Network.Interface.WiFi.2.Status",
            "Device.Network.Interface.Bridge.1.Name",
            "Device.Network.Interface.Bridge.1.Status",
            "Device.Network.Interface.Bridge.2.Name",
            "Device.Network.Interface.Bridge.2.Status",
            "Device.Network.Interface.Loopback.1.Name",
            "Device.Network.Interface.Loopback.1.Status",
        ])
        info = {p["Name"]: p["Value"] for p in params}

        lines.append("# HELP interface_up Interface operational (1) or down (0)")
        lines.append("# TYPE interface_up gauge")
        type_map = {"WiFi": "wifi", "Bridge": "bridge", "Loopback": "loopback"}
        for iftype in ("WiFi", "Bridge", "Loopback"):
            for inst in ("1", "2") if iftype != "Loopback" else ("1",):
                name_key = f"Device.Network.Interface.{iftype}.{inst}.Name"
                status_key = f"Device.Network.Interface.{iftype}.{inst}.Status"
                if name_key not in info:
                    continue
                name = info.get(name_key, f"{iftype}_{inst}")
                status = 1 if info.get(status_key, "") == "Enabled" else 0
                iftype_lower = type_map.get(iftype, iftype.lower())
                lines.append(f'interface_up{{name="{name}",type="{iftype_lower}"}} {status}')

    def _collect_ips(self, lines):
        params = self.client.get_values([
            "Device.Network.IP.1.IPv4Address.1.IPAddress",
            "Device.Network.IP.1.IPv4Address.1.AddressingType",
            "Device.Network.IP.2.IPv4Address.3.IPAddress",
            "Device.Network.IP.2.IPv4Address.3.AddressingType",
        ])
        info = {p["Name"]: p["Value"] for p in params}

        lines.append("# HELP lan_ip_info LAN IP address (static value)")
        lines.append("# TYPE lan_ip_info gauge")
        lan_ip = info.get("Device.Network.IP.1.IPv4Address.1.IPAddress", "")
        lan_type = info.get("Device.Network.IP.1.IPv4Address.1.AddressingType", "")
        lines.append(f'lan_ip_info{{address="{lan_ip}",type="{lan_type.lower()}"}} 1')

        lines.append("# HELP wan_ip_info WAN IP address (static value)")
        lines.append("# TYPE wan_ip_info gauge")
        wan_ip = info.get("Device.Network.IP.2.IPv4Address.3.IPAddress", "")
        wan_type = info.get("Device.Network.IP.2.IPv4Address.3.AddressingType", "")
        lines.append(f'wan_ip_info{{address="{wan_ip}",type="{wan_type.lower()}"}} 1')

    def _collect_switch_ports(self, lines):
        port_params = []
        for port in range(1, 6):
            port_params.append(f"Device.Switch.Ports.{port}.Status")
            port_params.append(f"Device.Switch.Ports.{port}.Alias")
        params = self.client.get_values(port_params)
        info = {p["Name"]: p["Value"] for p in params}

        lines.append("# HELP switch_port_enabled Switch port enabled (1) or disabled (0)")
        lines.append("# TYPE switch_port_enabled gauge")
        for port in range(1, 6):
            status = info.get(f"Device.Switch.Ports.{port}.Status", "Disabled")
            alias = info.get(f"Device.Switch.Ports.{port}.Alias", f"Port{port}")
            val = 1 if status == "Enabled" else 0
            lines.append(f'switch_port_enabled{{port="Port{port}",alias="{alias}"}} {val}')

    def _collect_wan(self, lines):
        params = self.client.get_values([
            "Device.Network.Connection.DHCP.2.ConnectionStatus",
            "Device.Network.Connection.DHCP.2.Status",
            "Device.Network.Connection.DHCP.2.Name",
        ])
        info = {p["Name"]: p["Value"] for p in params}

        lines.append("# HELP wan_connection_status WAN connection status")
        lines.append("# TYPE wan_connection_status gauge")
        conn_status = info.get("Device.Network.Connection.DHCP.2.ConnectionStatus", "Unknown")
        val = 1 if conn_status == "Connected" else 0
        lines.append(f'wan_connection_status{{status="{conn_status}"}} {val}')

    def _collect_system_time(self, lines):
        params = self.client.get_values([
            "Device.System.Time.Year",
            "Device.System.Time.Month",
            "Device.System.Time.Day",
            "Device.System.Time.Hour",
            "Device.System.Time.Minute",
        ])
        info = {p["Name"]: p["Value"] for p in params}
        try:
            dt = datetime(
                int(info.get("Device.System.Time.Year", 2024)),
                int(info.get("Device.System.Time.Month", 1)),
                int(info.get("Device.System.Time.Day", 1)),
                int(info.get("Device.System.Time.Hour", 0)),
                int(info.get("Device.System.Time.Minute", 0)),
                tzinfo=timezone.utc,
            )
            lines.append("# HELP system_time_seconds Current system time as unix timestamp")
            lines.append("# TYPE system_time_seconds gauge")
            lines.append(f"system_time_seconds {dt.timestamp():.0f}")
        except (ValueError, KeyError):
            pass

    def _collect_dhcp_leases(self, lines):
        """DHCP lease counts from Neighbours statistics."""
        # Device.Statistics.Neighbours is not accessible via CPE API on this model
        # This will be populated when web scraping is implemented
        lines.append("# HELP dhcp_lease_count Number of active DHCP leases")
        lines.append("# TYPE dhcp_lease_count gauge")
        lines.append("dhcp_lease_count 0")
        lines.append("# HELP dhcp_lease_info DHCP lease info (requires web scraping)")
        lines.append("# TYPE dhcp_lease_info gauge")
        lines.append('dhcp_lease_info{hostname="",ip="",mac=""} 0')

    # -- Web scraped metrics (requires Playwright) ---------------------------

    def add_web_scraped_metrics(self, lines, scraped: ScrapeResult):
        """Add metrics from Playwright web scraping."""
        self._add_interface_stats(lines, scraped.interfaces)
        self._add_port_stats(lines, scraped.ports)
        self._add_dhcp_leases(lines, scraped.dhcp_leases)
        self._add_clients(lines, scraped.clients)
        self._add_routes(lines, scraped.routes)
        self._add_wifi_clients(lines, scraped.wifi_clients, scraped.connected_clients_count)

    def _add_interface_stats(self, lines, interfaces: list[InterfaceStats]):
        if not interfaces:
            return
        lines.append("# HELP interface_rx_bytes Total bytes received on interface")
        lines.append("# TYPE interface_rx_bytes counter")
        for iface in interfaces:
            lines.append(f'interface_rx_bytes{{name="{iface.name}"}} {iface.rx_bytes}')
        lines.append("# HELP interface_tx_bytes Total bytes transmitted on interface")
        lines.append("# TYPE interface_tx_bytes counter")
        for iface in interfaces:
            lines.append(f'interface_tx_bytes{{name="{iface.name}"}} {iface.tx_bytes}')
        lines.append("# HELP interface_rx_errors Total receive errors on interface")
        lines.append("# TYPE interface_rx_errors counter")
        for iface in interfaces:
            lines.append(f'interface_rx_errors{{name="{iface.name}"}} {iface.rx_errors}')
        lines.append("# HELP interface_tx_errors Total transmit errors on interface")
        lines.append("# TYPE interface_tx_errors counter")
        for iface in interfaces:
            lines.append(f'interface_tx_errors{{name="{iface.name}"}} {iface.tx_errors}')

    def _add_port_stats(self, lines, ports: list[PortStats]):
        if not ports:
            return
        # Port traffic
        lines.append("# HELP port_rx_bytes Total bytes received on port")
        lines.append("# TYPE port_rx_bytes counter")
        for p in ports:
            lines.append(f'port_rx_bytes{{port="{p.port_id}",alias="{p.alias}"}} {p.bytes_received}')
        lines.append("# HELP port_tx_bytes Total bytes sent on port")
        lines.append("# TYPE port_tx_bytes counter")
        for p in ports:
            lines.append(f'port_tx_bytes{{port="{p.port_id}",alias="{p.alias}"}} {p.bytes_sent}')
        # Port link
        lines.append("# HELP port_link_up Port link status")
        lines.append("# TYPE port_link_up gauge")
        for p in ports:
            lines.append(f'port_link_up{{port="{p.port_id}",alias="{p.alias}",speed="{p.link_speed}"}} {int(p.link_up)}')
        # Port utilization
        lines.append("# HELP port_in_utilization_pct Port inbound utilization percent")
        lines.append("# TYPE port_in_utilization_pct gauge")
        for p in ports:
            lines.append(f'port_in_utilization_pct{{port="{p.port_id}"}} {p.in_util_pct}')
        lines.append("# HELP port_out_utilization_pct Port outbound utilization percent")
        lines.append("# TYPE port_out_utilization_pct gauge")
        for p in ports:
            lines.append(f'port_out_utilization_pct{{port="{p.port_id}"}} {p.out_util_pct}')

    def _add_dhcp_leases(self, lines, leases: list[DhcpLease]):
        lines.append("# HELP dhcp_lease_count Number of active DHCP leases")
        lines.append("# TYPE dhcp_lease_count gauge")
        lines.append(f"dhcp_lease_count {len(leases)}")
        if leases:
            lines.append("# HELP dhcp_lease_info DHCP lease information")
            lines.append("# TYPE dhcp_lease_info gauge")
            for l in leases:
                lines.append(f'dhcp_lease_info{{hostname="{l.hostname}",ip="{l.ip}",mac="{l.mac}"}} 1')

    def _add_clients(self, lines, clients: list[ClientInfo]):
        lines.append("# HELP wifi_client_count Number of connected WiFi clients")
        lines.append("# TYPE wifi_client_count gauge")
        lines.append(f"wifi_client_count {len(clients)}")
        if clients:
            lines.append("# HELP wifi_client_info Connected WiFi client info")
            lines.append("# TYPE wifi_client_info gauge")
            for c in clients:
                lines.append(f'wifi_client_info{{mac="{c.mac}",ip="{c.ip}",hostname="{c.hostname}",interface="{c.interface}"}} 1')

    def _add_wifi_clients(self, lines, clients: list[ClientInfo], total_count: int):
        """Add enriched WiFi client metrics from home page + client management."""
        lines.append("# HELP wifi_connected_clients_total Total number of connected WiFi clients")
        lines.append("# TYPE wifi_connected_clients_total gauge")
        lines.append(f"wifi_connected_clients_total {total_count}")

        if not clients:
            return

        lines.append("# HELP wifi_client_signal Signal strength of WiFi client (0-100%)")
        lines.append("# TYPE wifi_client_signal gauge")
        for c in clients:
            labels = f'mac="{c.mac}",hostname="{c.hostname}",ip="{c.ip}",ssid="{c.ssid}",band="{c.band}"'
            lines.append(f'wifi_client_signal{{{labels}}} {c.signal}')

        lines.append("# HELP wifi_client_band Frequency band of WiFi client connection")
        lines.append("# TYPE wifi_client_band gauge")
        for c in clients:
            if c.band == "2.4GHz":
                band_val = 1
            elif c.band == "5GHz":
                band_val = 2
            else:
                band_val = 0
            labels = f'mac="{c.mac}",hostname="{c.hostname}"'
            lines.append(f'wifi_client_band{{{labels}}} {band_val}')

        lines.append("# HELP wifi_client_online WiFi client is currently connected (1) or offline (0)")
        lines.append("# TYPE wifi_client_online gauge")
        for c in clients:
            labels = f'mac="{c.mac}",hostname="{c.hostname}",ip="{c.ip}",ssid="{c.ssid}"'
            lines.append(f'wifi_client_online{{{labels}}} 1')

    def _add_routes(self, lines, routes: list[RouteEntry]):
        lines.append("# HELP route_count Number of routing table entries")
        lines.append("# TYPE route_count gauge")
        lines.append(f"route_count {len(routes)}")
        if routes:
            lines.append("# HELP route_info Routing table entry")
            lines.append("# TYPE route_info gauge")
            for r in routes:
                lines.append(f'route_info{{destination="{r.destination}",gateway="{r.gateway}",netmask="{r.netmask}",interface="{r.interface}",metric="{r.metric}"}} 1')

    # ------------------------------------------------------------------
    # Syslog scraping (writes to file for Promtail)
    # ------------------------------------------------------------------

    def scrape_syslog(self, log_path: str, scraped: Optional[ScrapeResult] = None):
        """Scrape syslog from the router and write to a file."""
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now()

        # Write scraped syslog entries if available
        if scraped and scraped.syslog_entries:
            for entry in scraped.syslog_entries:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a") as f:
                    f.write(entry.rstrip() + "\n")
        else:
            # Heartbeat when no scraper available
            entry = f"{now.strftime('%b %d %H:%M:%S')} dlink-exporter[1]: syslog collector active (heartbeat)\n"
            try:
                with open(log_path, "a") as f:
                    f.write(entry)
            except OSError as e:
                log.warning("Cannot write syslog file %s: %s", log_path, e)

        # Rotate if too large
        try:
            if log_path.stat().st_size > DEFAULT_CONFIG["logging"]["log_max_size"]:
                log_path.rename(log_path.with_suffix(".log.1"))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HTTP Server (Prometheus /metrics endpoint)
# ---------------------------------------------------------------------------

class MetricsHandler(BaseHTTPRequestHandler):
    """Serves /metrics endpoint for Prometheus scraping."""

    def do_GET(self):
        if self.path == "/metrics":
            metrics = server_collector.get_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(metrics)))
            self.end_headers()
            self.wfile.write(metrics.encode())
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body>
                <h1>D-Link DIR-825 Exporter</h1>
                <p><a href="/metrics">Metrics</a></p>
                <p><a href="/health">Health</a></p>
                </body></html>
            """)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs


# Global reference for the handler
server_collector = None
server_config = None


def run_exporter(config):
    """Main loop: scrape metrics periodically and serve HTTP."""
    global server_collector, server_config
    server_config = config

    client = CpeClient(
        host=config["router"]["host"],
        username=config["router"]["username"],
        password=config["router"]["password"],
    )

    # Initial login
    if not client.login():
        log.error("Initial login failed. Exiting.")
        sys.exit(1)

    collector = MetricsCollector(client)
    server_collector = collector

    # Start Playwright scraper (optional - graceful fallback)
    scraper = None
    browser_service = os.environ.get("DLINK_BROWSER_SERVICE", "")
    try:
        scraper = DlinkScraper(
            host=config["router"]["host"],
            username=config["router"]["username"],
            password=config["router"]["password"],
            browser_service_url=browser_service,
        )
        scraper.start()
        if browser_service:
            log.info("Web scraper connected to remote browser service at %s", browser_service)
        else:
            log.info("Web scraper started (local Playwright)")
    except Exception as e:
        log.warning("Web scraper not available (install playwright + chromium): %s", e)
        log.warning("Metrics requiring web scraping will show placeholder values")

    # First scrape
    collector.scrape()

    # Start HTTP server
    addr = config["exporter"]["listen_address"]
    port = config["exporter"]["listen_port"]
    server = HTTPServer((addr, port), MetricsHandler)
    log.info("Exporter listening on %s:%d", addr, port)

    # Scrape loop
    last_metrics_scrape = time.time()
    last_log_scrape = time.time()
    last_browser_scrape = 0
    metrics_interval = config["exporter"]["scrape_interval"]
    log_interval = config["exporter"]["log_scrape_interval"]
    log_file = config["logging"]["log_file"]
    browser_scrape_interval = 60  # 1 minute for browser-based scraping

    last_scraped: Optional[ScrapeResult] = None

    try:
        while True:
            now = time.time()

            # Periodic browser-based scrape (every 5 min)
            if scraper and (now - last_browser_scrape >= browser_scrape_interval):
                try:
                    last_scraped = scraper.scrape_all()
                    last_browser_scrape = now
                    log.info("Browser scrape complete: %d interfaces, %d ports, %d leases, %d clients, %d routes, %d log lines",
                             len(last_scraped.interfaces), len(last_scraped.ports),
                             len(last_scraped.dhcp_leases), len(last_scraped.clients),
                             len(last_scraped.routes), len(last_scraped.syslog_entries))
                except Exception as e:
                    log.warning("Browser scrape failed, will retry: %s", e)
                    # Try to restart the scraper
                    try:
                        scraper.close()
                        scraper.start()
                    except Exception:
                        pass

            # Periodic metrics scrape (every 60s)
            if now - last_metrics_scrape >= metrics_interval:
                collector.scrape(scraped=last_scraped)
                last_metrics_scrape = now

            # Periodic syslog scrape (every 30s)
            if now - last_log_scrape >= log_interval:
                collector.scrape_syslog(log_file, scraped=last_scraped)
                last_log_scrape = now

            # Handle one HTTP request (blocks for up to 1 second)
            server.timeout = 1
            server.handle_request()

    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()
        if scraper:
            scraper.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    log = logging.getLogger("dlink-exporter")

    config = load_config()
    if not config["router"]["password"]:
        log.error("No router password set. Set DLINK_PASSWORD env or config.yaml")
        sys.exit(1)

    run_exporter(config)
