#!/usr/bin/env python3
"""rclone-rc-proxy — fixes boolean→string recursive parameter for rclone v1.75.0 RC API.

rclone v1.75.0 expects "recursive" as a string ("true"/"false"), but ZenLocalPoller v0.1.0
sends it as a boolean. This proxy converts the boolean to a string and forwards the
vfs/refresh call so the rclone VFS cache is actually populated with new content.
"""

import json
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

PORT_MAP = {
    5582: 5572, 5583: 5573, 5584: 5574, 5585: 5575,
    5586: 5576, 5587: 5577, 5588: 5578,
}
RC_HOST = "127.0.0.1"
RC_TIMEOUT = 120  # vfs/refresh for specific folders should be fast, but allow time for larger ones


def fix_body(body_bytes):
    """Convert boolean 'recursive' to string in JSON body."""
    if not body_bytes:
        return body_bytes
    try:
        data = json.loads(body_bytes)
        if "recursive" in data and isinstance(data["recursive"], bool):
            data["recursive"] = "true" if data["recursive"] else "false"
        return json.dumps(data).encode()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body_bytes


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_response(self, code, body, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method, body=None):
        target_port = PORT_MAP.get(self.server.server_address[1])
        if not target_port:
            self._send_response(502, b'{"error":"no target port"}')
            return

        # Fix boolean recursive -> string, then forward as-is (vfs/refresh stays vfs/refresh)
        if body:
            body = fix_body(body)

        url = f"http://{RC_HOST}:{target_port}{self.path}"
        req = Request(url, data=body, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=RC_TIMEOUT) as resp:
                self._send_response(resp.status, resp.read())
        except HTTPError as e:
            self._send_response(e.code, e.read())
        except URLError as e:
            self._send_response(502, json.dumps({"error": str(e)}).encode())
        except Exception as e:
            self._send_response(502, json.dumps({"error": str(e)}).encode())

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        self._proxy("POST", body)

    def do_GET(self):
        self._proxy("GET")

    def log_message(self, fmt, *args):
        pass


def main():
    servers = []
    for proxy_port, rc_port in PORT_MAP.items():
        server = ThreadingHTTPServer(("127.0.0.1", proxy_port), ProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        print(f"Proxy 127.0.0.1:{proxy_port} -> 127.0.0.1:{rc_port}", flush=True)
    print(f"rclone-rc-proxy running ({len(servers)} proxies)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    main()
