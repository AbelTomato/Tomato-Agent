"""Task 1 loopback probe. Credentials enter only via the parent's stdin pipe."""

import hmac
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROTOCOL = 1


def make_handler(token: str, instance: str, root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Never log request headers, query strings or credentials.
            pass

        def do_GET(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                self.send_error(401, "Unauthorized")
                return
            if self.headers.get("X-Tomato-Instance") != instance:
                self.send_error(409, "Instance mismatch")
                return
            if self.path == "/health":
                result = {"status": "ok", "protocol": PROTOCOL, "service_instance_id": instance}
            elif self.path == "/identity":
                result = {"protocol": PROTOCOL, "service_instance_id": instance, "root": str(root)}
            elif self.path == "/directory":
                result = {"root": str(root), "directories": sorted(
                    p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                )}
            else:
                self.send_error(404, "Not found")
                return
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    token = sys.stdin.buffer.readline(258).decode("ascii").strip()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise SystemExit("invalid desktop connection input")
    root = Path.cwd().resolve()
    instance = secrets.token_hex(16)
    server = HTTPServer(("127.0.0.1", 0), make_handler(token, instance, root))
    server.timeout = 1
    print(json.dumps({"protocol": PROTOCOL, "port": server.server_port,
                      "service_instance_id": instance}), flush=True)
    # This isolated probe is bounded, even if its owning desktop process crashes.
    import time
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()