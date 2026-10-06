"""The disposable relay must lose a receipt before Controller ingress."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ROOT = Path(__file__).resolve().parents[2]


def _relay(root: Path) -> tuple[ASGIApp, list[bytes]]:
    specification = importlib.util.spec_from_file_location(
        "acceptance_receipt_fault", ROOT / "tests/acceptance/receipt_fault.py"
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    accepted: list[bytes] = []

    async def controller(_scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        accepted.append(message.get("body", b""))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return module.LostStartReceipt(controller, root), accepted


def _post(relay: ASGIApp, document: dict[str, object]) -> list[int]:
    responses: list[int] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": json.dumps(document).encode()}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            responses.append(message["status"])

    asyncio.run(
        relay(
            {"type": "http", "method": "POST", "path": "/agent/result"},
            receive,
            send,
        )
    )
    return responses


def test_start_receipt_is_lost_before_controller_then_exact_retry_is_accepted(
    tmp_path: Path,
) -> None:
    (tmp_path / "armed.json").write_text("{}")
    relay, accepted = _relay(tmp_path)
    receipt: dict[str, object] = {
        "state": "succeeded",
        "fence": "11111111-1111-4111-8111-111111111111",
        "result": {"endpoint": "http://172.31.44.1:8000"},
    }
    assert _post(relay, receipt)[0] == 503
    assert _post(relay, receipt)[0] == 503
    assert accepted == []
    assert not (tmp_path / "replayed.json").exists()
    receipt["fence"] = "22222222-2222-4222-8222-222222222222"
    assert _post(relay, receipt)[0] == 204
    assert len(accepted) == 1
    assert json.loads((tmp_path / "replayed.json").read_text()) == {
        "fence": receipt["fence"],
        "endpoint": "http://172.31.44.1:8000",
    }


def test_non_start_receipt_is_not_interrupted(tmp_path: Path) -> None:
    (tmp_path / "armed.json").write_text("{}")
    relay, accepted = _relay(tmp_path)
    assert _post(relay, {"state": "succeeded", "result": {"bytes": 12}})[0] == 204
    assert len(accepted) == 1
    assert not (tmp_path / "blocked.json").exists()


def test_unarmed_receipt_is_not_interrupted(tmp_path: Path) -> None:
    relay, accepted = _relay(tmp_path)
    assert (
        _post(
            relay,
            {
                "state": "succeeded",
                "fence": "11111111-1111-4111-8111-111111111111",
                "result": {"endpoint": "http://172.31.44.1:8000"},
            },
        )[0]
        == 204
    )
    assert len(accepted) == 1
    assert not (tmp_path / "blocked.json").exists()
