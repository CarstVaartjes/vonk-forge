from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "dev-http-smoke"
HEALTH = {
    "status": "ok",
    "service": MODEL,
    "model": MODEL,
    "healthy": True,
}
EXPECTED_REQUEST = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "ping"}],
    "stream": False,
}
EXPECTED_RESPONSE = {
    "id": "chatcmpl-dev-http-smoke",
    "object": "chat.completion",
    "created": 1735689600,
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "dev-http-smoke ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5},
}


class Handler(BaseHTTPRequestHandler):
    server_version = "dev-http-smoke/1.0"

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self._write_json(200, HEALTH)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if payload != EXPECTED_REQUEST:
            self._write_json(
                400,
                {
                    "error": {
                        "message": "unexpected request",
                        "type": "invalid_request_error",
                    }
                },
            )
            return
        self._write_json(200, EXPECTED_RESPONSE)

    def log_message(self, format: str, *args) -> None:
        return

    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    host = os.environ["VONK_LISTEN_HOST"]
    port = int(os.environ["VONK_LISTEN_PORT"])
    ThreadingHTTPServer((host, port), Handler).serve_forever()
