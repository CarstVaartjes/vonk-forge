#!/usr/bin/env python3
"""Expose rank-0 model readiness on a headless worker rank."""

from __future__ import annotations

import argparse
import ipaddress
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_RESPONSE_BYTES = 1024 * 1024


class ReadinessHandler(BaseHTTPRequestHandler):
    upstream: str

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/v1/models":
            self.send_error(404)
            return
        try:
            with urllib.request.urlopen(self.upstream, timeout=5) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if response.status != 200 or len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("upstream readiness response is invalid")
                content_type = response.headers.get_content_type()
        except (OSError, ValueError, urllib.error.URLError):
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--master-address", required=True)
    parser.add_argument("--master-port", required=True, type=int)
    args = parser.parse_args()
    ipaddress.ip_address(args.master_address)
    if not (1024 <= args.listen_port <= 65535 and 1024 <= args.master_port <= 65535):
        parser.error("ports must be between 1024 and 65535")
    ReadinessHandler.upstream = (
        f"http://{args.master_address}:{args.master_port}/v1/models"
    )
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ReadinessHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
