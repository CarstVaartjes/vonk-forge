"""Bounded enrollment-body scanning and denial accounting."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_api import AgentApiServices, _EnrollmentGrantScan

from fastapi import HTTPException, Request

from .enrollment import (
    EnrollmentDenied,
)


def _json_string_end(value: bytes | bytearray, start: int) -> int | None:
    """Return the exclusive end of one bounded JSON string literal."""
    index = start + 1
    while index < len(value):
        byte = value[index]
        if byte == ord('"'):
            return index + 1
        if byte == ord("\\"):
            index += 2
        else:
            index += 1
    return None


def _skip_json_whitespace(value: bytes | bytearray, start: int) -> int:
    from .agent_api import _JSON_WHITESPACE

    while start < len(value) and value[start] in _JSON_WHITESPACE:
        start += 1
    return start


def _decode_bounded_json_string(
    value: bytes | bytearray,
    start: int,
    end: int,
    *,
    maximum_characters: int,
) -> str | None:
    # An ASCII target cannot require more than one six-byte \uXXXX escape per
    # character.  Reject longer candidates before making even a bounded copy.
    if end - start > 2 + (6 * maximum_characters):
        return None
    try:
        decoded = json.loads(bytes(value[start:end]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, str) or len(decoded) > maximum_characters:
        return None
    return decoded


def _scan_enrollment_grants(value: bytes | bytearray) -> _EnrollmentGrantScan:
    """Discover bounded grant strings without recursively parsing the body."""
    from .agent_api import _ENROLLMENT_TOKEN, _EnrollmentGrantScan

    tokens: list[str] = []
    seen: set[str] = set()
    top_level_keys = 0
    root_container: int | None = None
    depth = 0
    index = 0
    while index < len(value):
        byte = value[index]
        if byte == ord('"'):
            end = _json_string_end(value, index)
            if end is None:
                break
            colon = _skip_json_whitespace(value, end)
            if colon < len(value) and value[colon] == ord(":"):
                key = _decode_bounded_json_string(
                    value, index, end, maximum_characters=len("grant_token")
                )
                if key == "grant_token":
                    if root_container == ord("{") and depth == 1:
                        top_level_keys += 1
                    token_start = _skip_json_whitespace(value, colon + 1)
                    if token_start < len(value) and value[token_start] == ord('"'):
                        token_end = _json_string_end(value, token_start)
                        if token_end is not None:
                            token = _decode_bounded_json_string(
                                value,
                                token_start,
                                token_end,
                                maximum_characters=43,
                            )
                            if (
                                token is not None
                                and _ENROLLMENT_TOKEN.fullmatch(token) is not None
                                and token not in seen
                            ):
                                seen.add(token)
                                tokens.append(token)
            index = end
            continue
        if byte in (ord("{"), ord("[")):
            if root_container is None and depth == 0:
                root_container = byte
            depth += 1
        elif byte in (ord("}"), ord("]")) and depth > 0:
            depth -= 1
        index += 1
    return _EnrollmentGrantScan(tuple(tokens), top_level_keys)


def _consume_enrollment_denial(
    services: AgentApiServices, tokens: tuple[str, ...]
) -> None:
    from .agent_api import _require_enrollment

    if not tokens:
        return
    enrollment = _require_enrollment(services)
    for token in tokens:
        try:
            enrollment.submit(token, b"", {})
        except EnrollmentDenied:
            pass


async def _bounded_enrollment_body(
    request: Request, services: AgentApiServices
) -> bytearray:
    from .agent_api import (
        _MAX_ENROLLMENT_BODY_BYTES,
        _MAX_ENROLLMENT_TOKEN_PREFIX_BYTES,
    )

    buffered = bytearray()
    token_prefix = bytearray()
    async for chunk in request.stream():
        prefix_remaining = _MAX_ENROLLMENT_TOKEN_PREFIX_BYTES - len(token_prefix)
        if prefix_remaining > 0:
            token_prefix.extend(chunk[:prefix_remaining])
        remaining = _MAX_ENROLLMENT_BODY_BYTES - len(buffered)
        if len(chunk) > remaining:
            raise HTTPException(
                status_code=413, detail="enrollment request is too large"
            )
        buffered.extend(chunk)
    return buffered
