"""The shared retryable answer when a complete platform capture is unreadable."""

from typing import Literal

from starlette.responses import Response

from vonk_agent_protocol.error_response import (
    BoundedErrorResponse,
    ErrorContextResponse,
)
from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.categorized_errors import BookkeepingUnknown


class ObservationCaptureUnavailable(BookkeepingUnknown):
    """An observed capture failure, with no fabricated worker membership."""

    def __init__(
        self, *, phase: Literal["stored-worker-validation", "database-capture"]
    ) -> None:
        self.phase = phase
        super().__init__(
            f"Observation capture unavailable during {phase}; retry after repair.",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )


def observation_capture_unavailable_response(
    error: ObservationCaptureUnavailable, *, operation: str, endpoint: str
) -> Response:
    """Use the existing canonical bounded error model before streaming begins."""
    # Observers import the exception without importing public operation routes.

    problem = BoundedErrorResponse(
        detail=str(error),
        context=ErrorContextResponse(
            operation=operation,
            endpoint=endpoint,
            http_status=503,
            code=WaitReason.OBSERVATION_UNAVAILABLE.value,
            source="unknown",
            decision="retry",
            retryable=True,
        ),
    )
    return Response(
        content=canonical_message(problem.model_dump(mode="json", exclude_none=True)),
        status_code=503,
        media_type="application/json",
        headers={"Retry-After": "5", "Cache-Control": "no-store"},
    )
