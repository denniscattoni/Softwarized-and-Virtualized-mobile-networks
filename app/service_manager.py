#!/usr/bin/env python3
"""
ServiceManager REST API (management plane) for NFV service migration.

Goal:
- Start/stop a minimal HTTP streaming service on srv1/srv2 (DockerHost nodes).
- Keep dataplane (VIP/NAT + routing) fully controlled by the SDN controller.

API:
  GET  /service/status
  POST /service/start?backend=srv1|srv2
  POST /service/stop?backend=srv1|srv2
  POST /service/switch?to=srv1|srv2

Design goals:
- Debug-friendly: explicit shell commands, consistent logging.
- Deterministic: stop kills any previous instance before starting.
- Minimal dependencies: Python standard library only.

Content policy (build-time):
- The streaming payload (video.bin) is baked into the Docker image by the Dockerfile:
      COPY content/video.bin /var/www/html/video.bin
- The Makefile ensures app/content/video.bin exists before building the image.
- Therefore, at runtime we DO NOT generate payload; we hard-fail if it's missing.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


class ServiceManager:
    """
    Start/stop the backend service inside DockerHost namespaces via host.cmd().

    NOTE:
    - srv1/srv2 are DockerHost nodes (ComNetsEmu).
    - This manager runs in the same process that created the Mininet net object,
      so it can directly call srvX.cmd(...) without needing mnexec/PIDs.
    """

    def __init__(
        self,
        srv1,
        srv2,
        *,
        port: int = 8080,
        content_dir: str = "/var/www/html",
        resource: str = "video.bin",
        nginx_conf: str = "/etc/nginx/conf.d/streaming_demo.conf",
    ):
        self.srv1 = srv1
        self.srv2 = srv2

        self.port = int(port)
        self.content_dir = str(content_dir)
        self.resource = str(resource)
        self.nginx_conf = str(nginx_conf)

        # Runtime flags for observability (do not infer from process list)
        self.running = {"srv1": False, "srv2": False}

        # Serialize operations to keep "atomic switch" deterministic
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Backend operations
    # ------------------------------------------------------------------
    def start(self, backend: str):
        """
        Start nginx on the selected backend.

        Determinism:
          - Always stop any previous nginx processes first.
          - Always write a known config file.
          - Hard-fail if content payload is missing (must be in image).
        """
        assert backend in ("srv1", "srv2"), f"ServiceManager: invalid backend={backend}"
        host = self.srv1 if backend == "srv1" else self.srv2

        # Ensure base directory exists
        host.cmd(f"mkdir -p {self.content_dir}")

        # Content must exist in the container image (built by Makefile + Dockerfile COPY).
        res_path = os.path.join(self.content_dir, self.resource)
        rc = host.cmd(f"test -f {res_path}; echo $?").strip()
        assert rc.endswith("0"), (
            f"ServiceManager: missing content on {backend}: expected {res_path}. "
            f"Fix build pipeline (Makefile ensure-video + Dockerfile COPY)."
        )

        # Stop any previous nginx for determinism
        self._stop_nginx(host)

        # Write a minimal config that serves the payload on :8080
        # NOTE:
        # - We overwrite a dedicated file to avoid depending on distro defaults.
        host.cmd("mkdir -p /etc/nginx/conf.d")
        host.cmd(
            f"cat > {self.nginx_conf} <<'EOF'\n"
            "server {\n"
            f"    listen {self.port};\n"
            "    server_name _;\n"
            f"    root {self.content_dir};\n"
            "    location / {\n"
            "        autoindex on;\n"
            "    }\n"
            "}\n"
            "EOF\n"
        )

        # Start nginx (daemon mode)
        host.cmd("nginx >/dev/null 2>&1 &")

        self.running[backend] = True

    def stop(self, backend: str):
        """
        Stop nginx on the selected backend.
        """
        assert backend in ("srv1", "srv2"), f"ServiceManager: invalid backend={backend}"
        host = self.srv1 if backend == "srv1" else self.srv2
        self._stop_nginx(host)
        self.running[backend] = False

    def switch(self, to_backend: str):
        """
        Atomically switch service placement:
          - start(to_backend)
          - stop(other_backend)

        Why this exists:
          - reduces manual demo errors,
          - provides a single management-plane action for the controller (Step 3),
          - keeps behavior deterministic (serialized by self._lock).

        Important ordering:
          - Start first, then stop.
            This minimizes perceived downtime (even if dataplane reroute is applied right after).
        """
        assert to_backend in ("srv1", "srv2"), f"ServiceManager: invalid to_backend={to_backend}"
        other = "srv2" if to_backend == "srv1" else "srv1"

        with self._lock:
            self.start(to_backend)
            self.stop(other)

    def status(self) -> str:
        """
        Return a human-readable status string.
        """
        return f"srv1={'ON' if self.running['srv1'] else 'OFF'} srv2={'ON' if self.running['srv2'] else 'OFF'}"

    def _stop_nginx(self, host):
        """
        Best-effort nginx stop.

        We avoid systemctl here because DockerHost containers typically run without systemd.
        """
        host.cmd("pkill -f 'nginx: master' >/dev/null 2>&1 || true")
        host.cmd("pkill -f 'nginx: worker' >/dev/null 2>&1 || true")
        host.cmd("pkill -f '^nginx$' >/dev/null 2>&1 || true")


# -------------------------------------------------------------------
# REST server
# -------------------------------------------------------------------
def make_rest_handler(svc_mgr: ServiceManager):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/service/status":
                self._send(200, svc_mgr.status() + "\n")
                return
            self._send(404, "Not found\n")

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)

            if u.path == "/service/start":
                backend = (q.get("backend", [None])[0] or "").strip()
                try:
                    svc_mgr.start(backend)
                    self._send(200, f"OK start {backend}\n")
                except Exception as e:
                    self._send(400, f"ERROR {e}\n")
                return

            if u.path == "/service/stop":
                backend = (q.get("backend", [None])[0] or "").strip()
                try:
                    svc_mgr.stop(backend)
                    self._send(200, f"OK stop {backend}\n")
                except Exception as e:
                    self._send(400, f"ERROR {e}\n")
                return

            if u.path == "/service/switch":
                to_backend = (q.get("to", [None])[0] or "").strip()
                try:
                    svc_mgr.switch(to_backend)
                    self._send(200, f"OK switch to {to_backend}\n")
                except Exception as e:
                    self._send(400, f"ERROR {e}\n")
                return

            self._send(404, "Not found\n")

        def log_message(self, fmt, *args):
            # Keep Mininet output clean.
            return

    return Handler


def start_rest_server(svc_mgr: ServiceManager, *, host: str = "127.0.0.1", port: int = 9090):
    """
    Start REST server in a background thread.

    The server runs in the same Python process as the network runner,
    so it can call srvX.cmd(...) safely.
    """
    handler = make_rest_handler(svc_mgr)
    httpd = HTTPServer((host, int(port)), handler)

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    return httpd
