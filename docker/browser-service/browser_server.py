#!/usr/bin/env python3
"""
Browser sidecar service for D-Link DIR-825 exporter.

Runs Playwright + Chromium headless and exposes a simple HTTP API
for the main exporter to call. This keeps the exporter image small
and avoids bundling Chromium in it.

API:
  POST /login     - Log into the router and return session token
  POST /scrape    - Scrape a stats page, return rendered text

Usage: python browser_server.py
"""

import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

log = logging.getLogger("browser-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BROWSER = None
CONTEXT = None
PAGE = None


class BrowserHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps({"ok": True, "service": "browser-service"}).encode())
            except BrokenPipeError:
                pass  # Client disconnected — not an error
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                self.wfile.write(b"Not found")
            except BrokenPipeError:
                pass

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else "{}"
        params = json.loads(body) if body else {}

        if path == "/login":
            result = self._handle_login(params)
        elif path == "/scrape":
            result = self._handle_scrape(params)
        else:
            result = {"error": f"Unknown path: {path}"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(result).encode())
        except BrokenPipeError:
            pass

    def _handle_login(self, params):
        global PAGE
        host = params.get("host", "10.0.0.1")
        username = params.get("username", "admin")
        password = params.get("password", "")

        login_url = f"http://{host}/admin/index.html"
        try:
            PAGE.goto(login_url, wait_until="networkidle", timeout=15000)
            PAGE.wait_for_selector('input', timeout=10000)

            # Fill login form
            inputs = PAGE.locator("input").all()
            for inp in inputs:
                itype = (inp.get_attribute("type") or "").lower()
                placeholder = (inp.get_attribute("placeholder") or "").lower()
                if "password" in itype or "password" in placeholder:
                    inp.fill(password)
                elif itype == "text" or "username" in placeholder or not itype:
                    inp.fill(username)

            btn = PAGE.locator("button:has-text('Login')")
            if btn.count():
                btn.click()
            else:
                PAGE.keyboard.press("Enter")

            PAGE.wait_for_timeout(3000)
            return {"ok": True, "message": "Login successful"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _handle_scrape(self, params):
        global PAGE
        url = params.get("url", "")
        if not url:
            return {"error": "Missing 'url' parameter"}

        try:
            PAGE.goto(url, wait_until="networkidle", timeout=15000)
            PAGE.wait_for_timeout(3000)
            text = PAGE.locator("body").inner_text()
            return {"ok": True, "text": text, "url": url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs


def main():
    global BROWSER, CONTEXT, PAGE
    pw = sync_playwright().start()
    BROWSER = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    CONTEXT = BROWSER.new_context(viewport={"width": 1280, "height": 1024})
    PAGE = CONTEXT.new_page()

    port = 9200
    server = HTTPServer(("0.0.0.0", port), BrowserHandler)
    log.info("Browser service listening on :%d", port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()
        if CONTEXT:
            CONTEXT.close()
        if BROWSER:
            BROWSER.close()
        if pw:
            pw.stop()


if __name__ == "__main__":
    main()
