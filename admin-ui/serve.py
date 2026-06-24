#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import http.server
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class AdminUiHandler(http.server.SimpleHTTPRequestHandler):
    traefik_base_url = "http://127.0.0.1"
    traefik_host = "traefik.sunny"
    helper_base_url = "http://127.0.0.1:8092"

    def do_GET(self) -> None:
        if self.path.startswith("/traefik-api/"):
            self.proxy_traefik_api()
            return
        if self.path.startswith("/api/") or self.path.startswith("/admin-api/"):
            self.proxy_helper()
            return
        if self.path in {"/", "/index.html"} or "." in Path(urlparse(self.path).path).name:
            super().do_GET()
            return
        self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api/") or self.path.startswith("/admin-api/"):
            self.proxy_helper()
            return
        self.send_error(404)

    def do_OPTIONS(self) -> None:
        if self.path.startswith("/api/") or self.path.startswith("/admin-api/"):
            self.proxy_helper()
            return
        self.send_error(404)

    def proxy_traefik_api(self) -> None:
        proxied_path = self.path.replace("/traefik-api/", "/api/", 1)
        request = Request(f"{self.traefik_base_url}{proxied_path}", headers={"Host": self.traefik_host})
        try:
            with urlopen(request, timeout=5) as response:
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except HTTPError as error:
            body = error.read()
            self.send_response(error.code)
            self.send_header("Content-Type", error.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def proxy_helper(self) -> None:
        helper_path = self.path.removeprefix("/admin-api") if self.path.startswith("/admin-api") else self.path.removeprefix("/api")
        body = None
        if self.command in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None

        request = Request(
            f"{self.helper_base_url}{helper_path}",
            data=body,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        try:
            with urlopen(request, timeout=10) as response:
                response_body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
        except HTTPError as error:
            response_body = error.read()
            self.send_response(error.code)
            self.send_header("Content-Type", error.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the standalone Traefik Admin UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--traefik-base-url", default="http://127.0.0.1")
    parser.add_argument("--traefik-host", default="traefik.sunny")
    parser.add_argument("--helper-base-url", default="http://127.0.0.1:8092")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    AdminUiHandler.traefik_base_url = args.traefik_base_url.rstrip("/")
    AdminUiHandler.traefik_host = args.traefik_host
    AdminUiHandler.helper_base_url = args.helper_base_url.rstrip("/")
    handler = functools.partial(AdminUiHandler, directory=root)
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"traefik-admin-ui serving {root} on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
