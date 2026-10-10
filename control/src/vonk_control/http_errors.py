"""Explicit security refusals and standard temporary HTTP answers."""

from typing import Literal, NoReturn

from fastapi import HTTPException
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from vonk_agent_protocol.http_failure import HttpTransient, TransientReason

DEFAULT_RETRY_AFTER = 5


class SecurityHTTPError(HTTPException):
    """A real authentication or authorization edge; never a storage failure."""

    def __init__(self, *, status_code: Literal[401, 403], detail: object) -> None:
        if status_code not in {401, 403}:
            raise ValueError("security refusals require HTTP 401 or 403")
        super().__init__(status_code=status_code, detail=detail)


class TemporaryHTTPError(HTTPException):
    def __init__(self, answer: HttpTransient) -> None:
        self.answer = answer
        super().__init__(
            status_code=429 if answer.reason == TransientReason.RATE_LIMITED else 503,
            detail=answer.model_dump(mode="json"),
            headers={"retry-after": str(answer.retry_after)},
        )


def temporary_http_answer(
    reason: TransientReason, *, retry_after: int = DEFAULT_RETRY_AFTER
) -> NoReturn:
    """Raise a typed temporary answer without changing any security decision."""
    raise TemporaryHTTPError(HttpTransient(reason=reason, retry_after=retry_after))


class RetryAfterMiddleware:
    """Supply only a missing standard retry header on temporary statuses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def with_retry_after(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in {
                503,
                429,
            }:
                headers = MutableHeaders(scope=message)
                if "retry-after" not in headers:
                    headers["retry-after"] = str(DEFAULT_RETRY_AFTER)
            await send(message)

        await self.app(scope, receive, with_retry_after)
