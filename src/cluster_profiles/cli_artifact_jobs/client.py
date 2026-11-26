"""The artifact job transport seam and its bounded recovery of unknown effects."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from cluster_profiles.control_client.errors import (
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
    ControlResponseTooLarge,
    ControlTransportError,
    TransferUnavailable,
)
from cluster_profiles.control_client.transfers import TransferReceipt


@runtime_checkable
class ArtifactJobClient(Protocol):
    @property
    def request_timeout_seconds(self) -> float: ...

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]: ...

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]: ...

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
        timeout_seconds: float | None = None,
    ) -> TransferReceipt: ...


_RECOVERY_ATTEMPTS = 3


def _transfer_client(client: ArtifactJobClient) -> ArtifactJobClient:
    if not callable(getattr(client, "upload_file", None)) or not callable(
        getattr(client, "download_file", None)
    ):
        raise TransferUnavailable
    return client


def _transient_failure(error: BaseException) -> bool:
    """A pure read repeats only after a fault the next try can differ on."""
    if isinstance(error, ControlHTTPError):
        return error.status_code >= 500 or error.status_code in {408, 429}
    return _may_have_completed(error)


def _may_have_completed(error: BaseException) -> bool:
    if isinstance(error, ControlHTTPError):
        return error.status_code not in {401, 403}
    if isinstance(error, (ControlMalformedResponse, ControlResponseTooLarge)):
        status = error.context.http_status if error.context is not None else None
        return status not in {401, 403}
    return isinstance(error, (ControlClientError, OSError))


def _recover[T](
    client: ArtifactJobClient,
    send: Callable[[float], T],
    observe: Callable[[float], T | None],
    *,
    retry_if: Callable[[BaseException], bool] = _may_have_completed,
) -> T:
    """Reconcile an unknown effect, then replay only identical authorized intent.

    There is one total deadline and at most three sends and six observations.
    Unknown reads never become state-based admission decisions.
    """
    timeout = client.request_timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    deadline = time.monotonic() + 3 * timeout
    last: BaseException = ControlTransportError("artifact request outcome is unknown")

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise ControlTransportError(
                "artifact request observation deadline reached"
            ) from last
        return min(timeout, budget)

    for attempt in range(_RECOVERY_ATTEMPTS):
        try:
            return send(remaining())
        except (ControlClientError, OSError) as error:
            if not retry_if(error):
                raise
            last = error
        for _ in range(2):
            try:
                observed = observe(remaining())
                if observed is not None:
                    return observed
                break
            except ControlNotFound:
                break
            except (ControlClientError, OSError) as error:
                if not retry_if(error):
                    raise
                last = error
        delay = min(0.1 * 2**attempt, max(0.0, deadline - time.monotonic()))
        retry_after = getattr(last, "retry_after_seconds", None)
        if retry_after is not None:
            budget = max(0.0, deadline - time.monotonic())
            delay = max(delay, budget if retry_after >= budget else float(retry_after))
        if attempt + 1 < _RECOVERY_ATTEMPTS and delay > 0:
            time.sleep(delay)
    if isinstance(last, ControlHTTPError):
        raise last
    raise ControlTransportError(
        "artifact request outcome remains unknown after bounded reconciliation"
    ) from last
