from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self._stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self._stream.read(maximum)


def test_healthcheck_drops_privileges_then_probes_actual_readiness(monkeypatch) -> None:
    from vonk_control import healthcheck

    events: list[object] = []
    monkeypatch.setattr(
        healthcheck,
        "drop_runtime_privileges",
        lambda **kwargs: events.append(("drop", kwargs)),
    )

    def open_url(request, *, timeout: float):
        events.append(("request", request.full_url, timeout))
        return _Response({"status": "ready"})

    healthcheck.main(open_url=open_url)

    assert events == [
        ("drop", {"source_secrets": Path("/run/secrets")}),
        ("request", "http://127.0.0.1:8000/api/v1/readyz", 3),
    ]


@pytest.mark.parametrize(
    "response",
    (
        {"status": "wrong"},
        {"ready": True},
        ["ready"],
    ),
)
def test_healthcheck_rejects_non_ready_response(monkeypatch, response: object) -> None:
    from vonk_control import healthcheck

    monkeypatch.setattr(healthcheck, "drop_runtime_privileges", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="readiness response is invalid"):
        healthcheck.main(open_url=lambda *_args, **_kwargs: _Response(response))
