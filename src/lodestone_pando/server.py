"""Loopback-only UI. Explicit same-origin, per-process token for writes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlsplit

from .store import Board, encoded


def make_server(home, port=8765):
    board = Board(home)
    token = secrets.token_hex(32)
    template = (Path(__file__).parent / "static/index.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Request bodies and source content do not enter access logs.

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            raw = data.encode("utf-8") if isinstance(data, str) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers(); self.wfile.write(raw)

        def local_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.local_host():
                return self.send(403, encoded({"error": "Loopback Host required"}))
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    return self.send(200, template.replace("__PANDO_TOKEN__", token), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self.send(200, encoded(board.state()))
                if path.startswith("/api/sources/"):
                    return self.send(200, encoded(board.source(path.rsplit("/", 1)[1])))
                if path.startswith("/api/artifacts/"):
                    return self.send(200, encoded(board.artifact(path.rsplit("/", 1)[1])))
                self.send(404, encoded({"error": "Not found"}))
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.send(400, encoded({"error": str(exc)}))

        def do_POST(self):
            origins = (None, f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            supplied_token = self.headers.get("X-Pando-Token", "")
            if (not self.local_host() or self.headers.get("Origin") not in origins
                    or not supplied_token.isascii() or not secrets.compare_digest(supplied_token, token)):
                return self.send(403, encoded({"error": "Local write token and same origin required"}))
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Expected a bounded JSON body")
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete body")
                value = json.loads(raw)
                if type(value) is not dict:
                    raise ValueError("Expected a JSON object")
                if self.path != "/api/posts":
                    return self.send(404, encoded({"error": "Not found"}))
                self.send(201, encoded({"id": board.post(**value)}))
            except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
                self.send(400, encoded({"error": str(exc)}))

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(home, port=8765):
    server = make_server(home, port)
    print(encoded({"url": f"http://127.0.0.1:{server.server_port}", "scope": "This computer only (loopback)", "home": str(home)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
