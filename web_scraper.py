"""
Web scraper module for D-Link DIR-825 stats pages.

Uses Playwright (headless Chromium) to render Angular pages
and extract data that's not available via the CPE API.

The dsysinit data layer (Device.Statistics.*) is only accessible
through client-side Angular rendering. This module bridges that gap.
"""

import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger("dlink-exporter.scraper")


@dataclass
class InterfaceStats:
    name: str
    ip: str = ""
    gateway: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_errors: int = 0
    tx_errors: int = 0


@dataclass
class PortStats:
    port_id: str
    alias: str = ""
    link_speed: str = ""
    link_up: bool = False
    bytes_sent: int = 0
    bytes_received: int = 0
    packets_sent: int = 0
    packets_received: int = 0
    errors_sent: int = 0
    errors_received: int = 0
    in_util_pct: float = 0.0
    out_util_pct: float = 0.0


@dataclass
class DhcpLease:
    hostname: str = ""
    ip: str = ""
    mac: str = ""
    lease_seconds: int = 0


@dataclass
class ClientInfo:
    mac: str = ""
    ip: str = ""
    hostname: str = ""
    interface: str = ""
    ssid: str = ""
    band: str = ""
    signal: int = 0
    ipv6: str = ""


@dataclass
class RouteEntry:
    destination: str = ""
    gateway: str = ""
    netmask: str = ""
    interface: str = ""
    metric: str = ""


@dataclass
class WanInfo:
    """WAN connection info from home page dashboard."""
    ipv4_address: str = ""
    ipv4_status: str = ""
    ipv6_address: str = ""
    ipv6_status: str = ""
    connection_type: str = ""


@dataclass
class ScrapeResult:
    interfaces: list[InterfaceStats] = field(default_factory=list)
    ports: list[PortStats] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    clients: list[ClientInfo] = field(default_factory=list)
    routes: list[RouteEntry] = field(default_factory=list)
    syslog_entries: list[str] = field(default_factory=list)
    wifi_clients: list[ClientInfo] = field(default_factory=list)
    connected_clients_count: int = 0


class DlinkScraper:
    """Headless browser scraper for D-Link DIR-825 stats pages."""

    BASE_URL = "http://10.0.0.1"
    LOGIN_URL = f"{BASE_URL}/admin/index.html"

    STATS_PAGES = {
        "network": f"{BASE_URL}/admin/index.html#/control/stats/network",
        "ports": f"{BASE_URL}/admin/index.html#/control/stats/ports",
        "dhcp": f"{BASE_URL}/admin/index.html#/control/stats/dhcp",
        "clients": f"{BASE_URL}/admin/index.html#/control/stats/clients_sessions",
        "routing": f"{BASE_URL}/admin/index.html#/control/stats/routing",
        "syslog": f"{BASE_URL}/admin/index.html#/control/syslog",
    }

    def __init__(self, host: str, username: str, password: str):
        self.host = host
        self.BASE_URL = f"http://{host}"
        self.LOGIN_URL = f"{self.BASE_URL}/admin/index.html"
        self.STATS_PAGES = {
            k: f"{self.BASE_URL}/admin/index.html#/control/stats/{v}"
            for k, v in {
                "network": "network",
                "ports": "ports",
                "dhcp": "dhcp",
                "clients": "clients_sessions",
                "routing": "routing",
                "syslog": "syslog",
                "home": "",          # just #/home
                "clientmgm": "",     # /functions/wifi/clientmgm
            }.items()
        }
        # Override the special paths
        self._page_urls = {
            "home": f"{self.BASE_URL}/admin/index.html#/home",
            "clientmgm": f"{self.BASE_URL}/admin/index.html#/functions/wifi/clientmgm",
        }
        self.username = username
        self.password = password
        self._browser = None
        self._context = None
        self._page = None

    def start(self):
        """Launch Playwright browser and login."""
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 1024},
            locale="en-US",
        )
        self._page = self._context.new_page()
        self._login()
        log.info("Browser started and logged in")

    def _login(self):
        """Log into the router admin interface."""
        page = self._page
        page.goto(self.LOGIN_URL, wait_until="networkidle", timeout=15000)

        # Wait for login form to render
        page.wait_for_selector('input[type="text"], input:not([type])', timeout=10000)

        # The D-Link login has username/password inputs
        # Find and fill them
        inputs = page.locator("input").all()
        username_input = None
        password_input = None
        for inp in inputs:
            input_type = inp.get_attribute("type") or ""
            placeholder = (inp.get_attribute("placeholder") or "").lower()
            if "password" in input_type or "password" in placeholder:
                password_input = inp
            elif "text" in input_type or "username" in placeholder or not input_type:
                username_input = inp

        if username_input:
            username_input.fill(self.username)
        if password_input:
            password_input.fill(self.password)

        # Click login button
        login_btn = page.locator("button:has-text('Login')")
        if login_btn.count():
            login_btn.click()
        else:
            # Try submitting the form
            page.keyboard.press("Enter")

        # Wait for dashboard to load
        page.wait_for_timeout(3000)
        log.info("Login submitted")

    def scrape_all(self) -> ScrapeResult:
        """Scrape all stats pages and return combined result."""
        result = ScrapeResult()

        try:
            result.interfaces = self._scrape_network_stats()
        except Exception as e:
            log.warning("Network stats scrape failed: %s", e)

        try:
            result.ports = self._scrape_port_stats()
        except Exception as e:
            log.warning("Port stats scrape failed: %s", e)

        try:
            result.dhcp_leases = self._scrape_dhcp_leases()
        except Exception as e:
            log.warning("DHCP scrape failed: %s", e)

        try:
            result.clients = self._scrape_clients()
        except Exception as e:
            log.warning("Clients scrape failed: %s", e)

        try:
            result.routes = self._scrape_routing()
        except Exception as e:
            log.warning("Routing scrape failed: %s", e)

        try:
            result.syslog_entries = self._scrape_syslog()
        except Exception as e:
            log.warning("Syslog scrape failed: %s", e)

        # WiFi-specific pages
        try:
            wifi_clients = self._scrape_home_wifi_clients()
            result.wifi_clients = wifi_clients
            result.connected_clients_count = len(wifi_clients)
        except Exception as e:
            log.warning("Home page wifi scrape failed: %s", e)

        try:
            detailed = self._scrape_client_mgmt()
            # Merge signal/band/ssid from detailed view into wifi_clients
            if detailed and result.wifi_clients:
                detail_map = {}
                for c in detailed:
                    detail_map[c.mac.upper()] = c
                for c in result.wifi_clients:
                    d = detail_map.get(c.mac.upper())
                    if d:
                        c.signal = d.signal
                        c.band = d.band
                        if d.ssid:
                            c.ssid = d.ssid
            elif detailed and not result.wifi_clients:
                result.wifi_clients = detailed
                result.connected_clients_count = len(detailed)
        except Exception as e:
            log.warning("Client management scrape failed: %s", e)

        return result

    def _scrape_network_stats(self) -> list[InterfaceStats]:
        """Scrape Network Statistics page."""
        page = self._page
        page.goto(self.STATS_PAGES["network"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        interfaces = []
        # Look for rows in the stats table
        rows = page.locator("table tr, .stats-row, [ng-repeat*=iface]").all()
        for row in rows:
            cells = row.locator("td, .stats-cell").all()
            texts = [c.inner_text().strip() for c in cells]
            if len(texts) >= 3 and texts[0]:
                iface = InterfaceStats(name=texts[0])
                if len(texts) >= 2:
                    iface.ip = texts[1]
                if len(texts) >= 3:
                    iface.gateway = texts[2]
                # Try to parse rx/tx bytes from later cells
                for t in texts[3:]:
                    t = t.replace(",", "").replace(" ", "")
                    if t.isdigit():
                        if not iface.rx_bytes:
                            iface.rx_bytes = int(t)
                        elif not iface.tx_bytes:
                            iface.tx_bytes = int(t)
                interfaces.append(iface)

        return interfaces

    def _scrape_port_stats(self) -> list[PortStats]:
        """Scrape Port Statistics page."""
        page = self._page
        page.goto(self.STATS_PAGES["ports"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        ports = []
        rows = page.locator("table tr, .stats-row, [ng-repeat*=port]").all()
        for row in rows:
            cells = row.locator("td, .stats-cell").all()
            texts = [c.inner_text().strip() for c in cells]
            if not texts or not texts[0]:
                continue
            port = PortStats(port_id=texts[0])
            for i, t in enumerate(texts[1:], 1):
                t_clean = t.replace(",", "").replace(" ", "")
                if "M-" in t or "Full" in t or "Half" in t:
                    port.link_speed = t
                elif t.lower() in ("connected", "up"):
                    port.link_up = True
                elif t.lower() in ("disconnected", "down"):
                    port.link_up = False
                elif t_clean.isdigit():
                    val = int(t_clean)
                    if not port.bytes_sent and not port.bytes_received:
                        port.bytes_received = val
                    elif not port.bytes_sent:
                        port.bytes_sent = val
                    elif not port.packets_received:
                        port.packets_received = val
                    elif not port.packets_sent:
                        port.packets_sent = val
            ports.append(port)

        return ports

    def _scrape_dhcp_leases(self) -> list[DhcpLease]:
        """Scrape DHCP Leases page."""
        page = self._page
        page.goto(self.STATS_PAGES["dhcp"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        leases = []
        rows = page.locator("table tr, .lease-row, [ng-repeat*=lease]").all()
        for row in rows:
            cells = row.locator("td, .lease-cell").all()
            texts = [c.inner_text().strip() for c in cells]
            if len(texts) >= 2:
                lease = DhcpLease()
                if texts[0]:
                    lease.hostname = texts[0]
                if len(texts) >= 2:
                    lease.ip = texts[1]
                if len(texts) >= 3:
                    lease.mac = texts[2]
                leases.append(lease)

        return leases

    def _scrape_clients(self) -> list[ClientInfo]:
        """Scrape Clients and Sessions page."""
        page = self._page
        page.goto(self.STATS_PAGES["clients"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        clients = []
        rows = page.locator("table tr, .client-row, [ng-repeat*=client]").all()
        for row in rows:
            cells = row.locator("td, .client-cell").all()
            texts = [c.inner_text().strip() for c in cells]
            if len(texts) >= 2:
                client = ClientInfo()
                if texts[0]:
                    client.mac = texts[0]
                if len(texts) >= 2:
                    client.ip = texts[1]
                if len(texts) >= 3:
                    client.hostname = texts[2]
                clients.append(client)

        return clients

    def _scrape_routing(self) -> list[RouteEntry]:
        """Scrape Routing Statistics page."""
        page = self._page
        page.goto(self.STATS_PAGES["routing"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        routes = []
        rows = page.locator("table tr, .route-row, [ng-repeat*=route]").all()
        for row in rows:
            cells = row.locator("td, .route-cell").all()
            texts = [c.inner_text().strip() for c in cells]
            if len(texts) >= 2:
                route = RouteEntry()
                if texts[0]:
                    route.destination = texts[0]
                if len(texts) >= 2:
                    route.gateway = texts[1]
                if len(texts) >= 3:
                    route.netmask = texts[2]
                if len(texts) >= 4:
                    route.interface = texts[3]
                if len(texts) >= 5:
                    route.metric = texts[4]
                routes.append(route)

        return routes

    def _scrape_syslog(self) -> list[str]:
        """Scrape System Log page."""
        page = self._page
        page.goto(self.STATS_PAGES["syslog"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        entries = []
        log_entries = page.locator(".log-entry, .syslog-row, [ng-repeat*=log], pre, code").all()
        for entry in log_entries:
            text = entry.inner_text().strip()
            if text and len(text) > 10:
                entries.append(text)

        # If no specific log elements found, try the page content directly
        if not entries:
            body_text = page.locator("body").inner_text()
            for line in body_text.split("\n"):
                line = line.strip()
                if line and len(line) > 15:
                    entries.append(line)

        return entries

    # -- WiFi client pages ---------------------------------------------------

    def _scrape_home_wifi_clients(self) -> list[ClientInfo]:
        """Scrape Home page for connected WiFi clients.
        
        Home page renders a client table with: MAC, IPv4, IPv6, Hostname, SSID.
        """
        page = self._page
        page.goto(self._page_urls["home"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        clients = []
        # Home page shows client cards with MAC/IP/hostname info
        # Try multiple selectors for client elements
        client_cards = page.locator(
            ".client-card, .client-item, [ng-repeat*=client], "
            ".clients-table [ng-repeat*=item], .client-info, "
            "[class*=client] [class*=mac], [class*=client] [class*=ip]"
        ).all()

        # Parse visible text for MAC addresses and associated data
        body_text = page.locator("body").inner_text()
        lines = body_text.split("\n")
        current = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Match MAC addresses (XX:XX:XX:XX:XX:XX)
            mac_match = re.search(r'([0-9A-Fa-f]{2}[:.-]){5}[0-9A-Fa-f]{2}', line)
            if mac_match:
                if current.get("mac"):
                    client = ClientInfo(mac=current["mac"])
                    client.hostname = current.get("hostname", "")
                    client.ip = current.get("ip", "")
                    clients.append(client)
                current = {"mac": mac_match.group(0).upper()}
            elif current:
                # Check for IP address
                ip_match = re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', line)
                if ip_match:
                    current["ip"] = ip_match.group(0)
                elif line and len(line) < 50 and not line.startswith("http"):
                    current["hostname"] = line

        # Don't forget the last one
        if current.get("mac"):
            client = ClientInfo(mac=current["mac"])
            client.hostname = current.get("hostname", "")
            client.ip = current.get("ip", "")
            clients.append(client)

        # Check for "Connected Clients" count
        count_match = re.search(r'(?:connected|clients)\s*[: ]\s*(\d+)', body_text, re.IGNORECASE)
        if count_match:
            self._connected_count = int(count_match.group(1))

        return clients

    def _scrape_client_mgmt(self) -> list[ClientInfo]:
        """Scrape WiFi Client Management page for detailed client info.
        
        Shows per-client: MAC, Hostname, SSID, Band (2.4GHz/5GHz),
        Signal strength (0-100%), IPv4, IPv6.
        """
        page = self._page
        page.goto(self._page_urls["clientmgm"], wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)

        clients = []
        body_text = page.locator("body").inner_text()
        lines = body_text.split("\n")

        current = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            mac_match = re.search(r'([0-9A-Fa-f]{2}[:.-]){5}[0-9A-Fa-f]{2}', line)
            if mac_match:
                if current.get("mac"):
                    clients.append(self._build_client(current))
                current = {"mac": mac_match.group(0).upper()}
            elif current:
                # Signal strength - percentage or "X%" or "Signal: X"
                sig_match = re.search(r'(\d{1,3})\s*%', line)
                if sig_match:
                    current["signal"] = int(sig_match.group(1))
                # Band detection
                if "2.4" in line and "GHz" in line:
                    current["band"] = "2.4GHz"
                elif "5" in line and "GHz" in line:
                    current["band"] = "5GHz"
                # SSID detection (not MAC, not IP, reasonable length)
                ip_match = re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', line)
                if ip_match and "ip" not in current:
                    current["ip"] = ip_match.group(0)
                # Simple SSID heuristic: text between 2-30 chars not matching other patterns
                elif (2 < len(line) < 30 and line != current.get("mac", "")
                      and not line.startswith("http") and not line.startswith("192.")
                      and not re.match(r'^\d+%$', line)
                      and "hostname" not in current):
                    current["hostname"] = line

        if current.get("mac"):
            clients.append(self._build_client(current))

        return clients

    def _build_client(self, raw: dict) -> ClientInfo:
        """Build ClientInfo from raw parsed dict."""
        return ClientInfo(
            mac=raw.get("mac", ""),
            hostname=raw.get("hostname", ""),
            ip=raw.get("ip", ""),
            ssid=raw.get("ssid", ""),
            band=raw.get("band", ""),
            signal=raw.get("signal", 0),
            ipv6=raw.get("ipv6", ""),
        )

    def close(self):
        """Clean up browser resources."""
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
