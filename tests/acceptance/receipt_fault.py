"""Disposable acceptance-only relay for one lost Start receipt.

Mounted into the isolated Controller by the ARM lifecycle harness. This module
is never included in the Controller image and has no production configuration.
The Controller remains the claim/retry authority; the relay never writes SQL.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from vonk_agent_protocol import (
    AgentResult,
    OutcomeDone,
    RecipeStartResult,
    outcome_body,
)

_FENCE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_MAXIMUM_BYTES = 64 * 1024


def _write(path: Path, document: dict[str, str]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o644)
        json.dump(document, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _read(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size > _MAXIMUM_BYTES:
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    return document if isinstance(document, dict) else {}


def _start_receipt(raw: bytes) -> dict[str, str] | None:
    try:
        document = AgentResult.model_validate_json(raw, strict=True)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(document.result, OutcomeDone):
        return None
    result = outcome_body(document.result)
    if (
        not isinstance(result, RecipeStartResult)
        or _FENCE.fullmatch(document.fence) is None
        or result.endpoint is None
        or not 1 <= len(result.endpoint) <= 512
    ):
        return None
    return {"fence": document.fence, "endpoint": result.endpoint}


class LostStartReceipt:
    def __init__(self, app: ASGIApp, root: Path) -> None:
        self.app = app
        self.root = root

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/agent/result"
            or not (self.root / "armed.json").is_file()
        ):
            await self.app(scope, receive, send)
            return
        messages: list[Message] = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if len(body) > _MAXIMUM_BYTES or not message.get("more_body", False):
                break
        pending = iter(messages)

        async def replay() -> Message:
            return next(pending, None) or await receive()

        receipt = _start_receipt(bytes(body)) if len(body) <= _MAXIMUM_BYTES else None
        if receipt is None:
            await self.app(scope, replay, send)
            return
        blocked_path = self.root / "blocked.json"
        blocked = _read(blocked_path)
        if not blocked:
            _write(blocked_path, receipt)
            blocked = dict(receipt)
        # A retry may arrive while the harness probes the still-serving effect.
        # Keep every Start receipt out of Controller ingress until journal loss
        # and the real agent restart have actually completed.
        if (
            receipt["fence"] == blocked.get("fence")
            or not (self.root / "recovered.json").is_file()
        ):
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": b"acceptance: receipt delivery interrupted",
                }
            )
            return

        async def observe(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] == 204
                and receipt["endpoint"] == blocked.get("endpoint")
            ):
                _write(self.root / "replayed.json", receipt)
            await send(message)

        await self.app(scope, replay, observe)


if __name__ == "__main__":
    import uvicorn
    from vonk_control.api import production_app

    uvicorn.run(
        LostStartReceipt(production_app(), Path("/acceptance-state")),
        host="0.0.0.0",
        port=8000,
        access_log=False,
    )
