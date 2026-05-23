#!/usr/bin/env python3
"""Mini App proxy — serves the Telegram Mini App and proxies API calls to the WebUI."""
import gzip
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

WEB_DIST = Path(__file__).parent.parent / "frontend"
API_BASE = "http://localhost:8787"
DASHBOARD_API = "http://localhost:8889"
PORT = 8789


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/"):
            # Backtest + Dashboard API routes go to the dashboard API server
            if parsed.path in ("/api/chart-data", "/api/symbols", 
                               "/api/backtest/progress", "/api/backtest/report",
                               "/api/backtest/start", "/api/backtest/cancel", "/api/fear-greed", "/api/backtest/history", "/api/backtest/history-file", "/api/backtest/active", "/api/backtest/cancel") or parsed.path == "/api/symbols":
                self._proxy(parsed, DASHBOARD_API)
            else:
                self._proxy(parsed, API_BASE)
            return

        # Resolve the file path to serve (strip query string)
        serve_path = parsed.path
        if serve_path in ('/', ''):
            serve_path = '/telegram-mini-app.html'
        if serve_path in ('/dashboard', '/backtest'):
            serve_path = serve_path + '.html'

        file_path = WEB_DIST / serve_path.lstrip('/')
        if file_path.exists() and file_path.is_file() and file_path.resolve().is_relative_to(WEB_DIST.resolve()):
            content = file_path.read_bytes()
            ct = "text/html" if file_path.suffix == ".html" else "text/css" if file_path.suffix == ".css" else "application/javascript" if file_path.suffix == ".js" else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", f"{ct}; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if parsed.path in ("/api/chart-data", "/api/symbols",
                               "/api/backtest/progress", "/api/backtest/report",
                               "/api/backtest/start", "/api/backtest/cancel"):
                self._proxy(parsed, DASHBOARD_API)
            else:
                self._proxy(parsed, API_BASE)
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _proxy(self, parsed, api_base=None):
        base = api_base or API_BASE
        url = f"{base}{parsed.path}"
        if parsed.query:
            url += f"?{parsed.query}"

        body = None
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            body = self.rfile.read(content_length)

        req = Request(url, data=body, method=self.command)
        # Don't forward Accept-Encoding so we get uncompressed from localhost
        for key, val in self.headers.items():
            if key.lower() not in ("host", "content-length", "accept-encoding"):
                req.add_header(key, val)

        try:
            with urlopen(req, timeout=30) as resp:
                raw_body = resp.read()
                content_type = resp.headers.get("Content-Type", "application/json")
                content_encoding = resp.headers.get("Content-Encoding", "")

                # Decompress if needed
                if content_encoding == "gzip" or (len(raw_body) >= 2 and raw_body[:2] == b'\x1f\x8b'):
                    try:
                        raw_body = gzip.decompress(raw_body)
                    except Exception:
                        pass

                self.send_response(resp.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(raw_body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), ProxyHandler)
    print(f"Proxy running on :{PORT} → {API_BASE}")
    server.serve_forever()