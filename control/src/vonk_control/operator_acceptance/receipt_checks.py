"""The refusals every operator acceptance-receipt resolver raises."""

from __future__ import annotations

from fastapi import HTTPException

from vonk_control.operator_acceptance.receipt_contract import ReceiptCode


def _refused(detail: str, *, unavailable: bool = False) -> HTTPException:
    code = ReceiptCode.UNAVAILABLE if unavailable else ReceiptCode.MISMATCH
    return HTTPException(
        status_code=503 if unavailable else 409,
        detail=detail,
        headers={
            "x-vonk-error-code": code,
            **({"retry-after": "5"} if unavailable else {}),
        },
    )


def _require(condition: bool) -> None:  # noqa: FBT001 -- a positional callback or assert-style helper signature
    if not condition:
        raise _refused("The owner has no matching acceptance receipt for this request")


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _refused("The response has no accepted request identity")
    return value


def _available[T](value: T | None) -> T:
    if value is None:
        raise _refused(
            "The receipt owner is unavailable; retry the same request", unavailable=True
        )
    return value
