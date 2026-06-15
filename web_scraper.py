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


@dataclass
class RouteEntry:
    destination: str = ""
    gateway: str = ""
    netmask: str = ""
    interface: str = ""
    metric: str = ""


@dataclass
class ScrapeResult:
    interfaces: list[InterfaceStats] = field(default_factory=list)
    ports: list[PortStats] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    clients: list[ClientInfo] = field(default_factory=list)
    routes: list[RouteEntry] = field(default_factory=list)
    syslog_entries: list[str] = field(default_factory=list)


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
            }.items()
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

    def close(self):
        """Clean up browser resources."""
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
