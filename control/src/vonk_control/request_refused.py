"""A contract verdict on an agent request's own bytes."""

from vonk_agent_protocol import InvalidRequestError, InvalidRequestReason


class RequestBytesRefused(InvalidRequestError, ValueError):
    """The request's own bytes can never be accepted; the agent may discard them.

    Agent routes answer 422 only for this (or a pydantic ValidationError). Any
    other ValueError is this Controller's own fault and is answered as a wait.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=InvalidRequestReason.MALFORMED)
