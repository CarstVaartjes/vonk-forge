"""Read actual TestClient streams through the installed production CLI receiver."""

import io
import tempfile
from email.message import Message
from pathlib import Path
from typing import Self

from httpx import Response
from httpx2 import Response as Response2

from cluster_profiles.control_client import ControlClient

type ObservationHTTPResponse = Response | Response2


class ObservationHTTPPeer:
    def __init__(self, response: ObservationHTTPResponse) -> None:
        self.status = response.status_code
        self.headers = Message()
        for key, value in response.headers.items():
            self.headers[key] = value
        self._body = io.BytesIO(response.content)

    def read(self, amount: int, /) -> bytes:
        return self._body.read(amount)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self._body.close()


def observation_document(response: ObservationHTTPResponse) -> dict[str, object]:
    """Exercise real envelope, receipt and bundled canonical-schema validation."""
    with tempfile.TemporaryDirectory() as directory:
        token = Path(directory) / "token"
        token.write_text("fixture-token")
        token.chmod(0o600)
        client = ControlClient(
            "https://control.invalid",
            token,
            opener=lambda _request, _timeout=None, **_kwargs: ObservationHTTPPeer(
                response
            ),
        )
        return client.request("GET", response.request.url.path)
