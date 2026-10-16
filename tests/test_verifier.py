from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from devops_agent.core.verifier import verify


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/style.css":
            body = b"body{}"
        else:
            body = b'<html><link rel="stylesheet" href="/style.css"></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/css" if self.path.endswith(".css") else "text/html")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_http_smoke_checks_main_page_and_asset() -> None:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        blocked = verify(url)
        assert blocked.verdict == "FAILED"
        allowed = verify(url, allow_private=True)
        assert allowed.verdict == "HEALTHY"
        assert any(check.name == "asset" and check.state == "PASSED" for check in allowed.checks)
    finally:
        server.shutdown()
        thread.join(timeout=2)
