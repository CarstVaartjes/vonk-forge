from __future__ import annotations

import io
import json

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


def test_healthcheck_probes_actual_readiness() -> None:
    from vonk_control import healthcheck
    from vonk_control.operation_api import ReadyzResponse

    events: list[object] = []

    def open_url(request, *, timeout: float):
        events.append(("request", request.full_url, timeout))
        return _Response(ReadyzResponse(status="ready").model_dump(mode="json"))

    healthcheck.main(open_url=open_url)

    assert events == [("request", "http://127.0.0.1:8000/api/readyz", 3)]


@pytest.mark.parametrize(
    "response",
    (
        {"status": "wrong"},
        {"ready": True},
        ["ready"],
    ),
)
def test_healthcheck_rejects_non_ready_response(response: object) -> None:
    from vonk_control import healthcheck

    with pytest.raises(Exception) as _ending:
        healthcheck.main(open_url=lambda *_args, **_kwargs: _Response(response))
    from vonk_control.operation_api import ReadyzResponse

    healthcheck.main(
        open_url=lambda *_args, **_kwargs: _Response(
            ReadyzResponse(status="ready").model_dump(mode="json")
        )
    )
