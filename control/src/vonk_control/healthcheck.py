"""Unprivileged container-local API readiness check."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, Self

from .api_preexec import drop_runtime_privileges

_READINESS_URL = "http://127.0.0.1:8000/api/v1/readyz"
_MAXIMUM_RESPONSE_BYTES = 256


class _Response(Protocol):
    status: int

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> object: ...

    def read(self, maximum: int) -> bytes: ...


def main(
    *,
    open_url: Callable[..., _Response] = urllib.request.urlopen,
) -> None:
    drop_runtime_privileges(source_secrets=Path("/run/secrets"))
    request = urllib.request.Request(_READINESS_URL, method="GET")
    with open_url(request, timeout=3) as response:
        content = response.read(_MAXIMUM_RESPONSE_BYTES + 1)
        if response.status != 200 or len(content) > _MAXIMUM_RESPONSE_BYTES:
            raise RuntimeError("API readiness response is invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("API readiness response is invalid") from error
    if payload != {"status": "ready"}:
        raise RuntimeError("API readiness response is invalid")


if __name__ == "__main__":
    main()
