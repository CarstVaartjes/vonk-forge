"""Db: the typed cause of a failed PostgreSQL connection."""

import re
import socket
from enum import StrEnum

from sqlalchemy.exc import DBAPIError


class ConnectionCause(StrEnum):
    NAME_RESOLUTION = "name_resolution_failed"
    CONNECTION_REFUSED = "connection_refused"
    TIMEOUT = "timeout"
    SERVER_CLOSED = "server_closed_connection"
    AUTHENTICATION_FAILED = "authentication_failed"
    DATABASE_MISSING = "database_missing"
    OTHER = "other"


_SQLSTATE_CAUSES = {
    "28P01": ConnectionCause.AUTHENTICATION_FAILED,
    "28000": ConnectionCause.AUTHENTICATION_FAILED,
    "3D000": ConnectionCause.DATABASE_MISSING,
}
# libpq and psycopg expose no structured code for failed establishment, so the
# message is the last resort after the exception chain and SQLSTATE.
_TEXT_CAUSES = (
    (
        (
            "translate host name",
            "nodename nor servname",
            "name or service not known",
            "name resolution",
            "failed to resolve host",
        ),
        ConnectionCause.NAME_RESOLUTION,
    ),
    (("connection refused",), ConnectionCause.CONNECTION_REFUSED),
    (("timeout expired", "timed out"), ConnectionCause.TIMEOUT),
    (("server closed the connection",), ConnectionCause.SERVER_CLOSED),
    (("password authentication failed",), ConnectionCause.AUTHENTICATION_FAILED),
    (("does not exist",), ConnectionCause.DATABASE_MISSING),
)
_SECRET = re.compile(r"(://[^:/@\s]*:)[^@\s]*@|(password\s*=\s*)\S+", re.IGNORECASE)


def _chain(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending and len(seen) < 8:
        item = pending.pop()
        if item is None or item in seen:
            continue
        seen.append(item)
        pending += [getattr(item, "orig", None), item.__cause__, item.__context__]
    return seen


def classify_connection_failure(error: BaseException) -> tuple[ConnectionCause, str]:
    """The cause and a credential-free message of a failed connection."""
    chain = _chain(error)
    driver = error.orig if isinstance(error, DBAPIError) and error.orig else error
    text = " ".join(str(driver).split())
    text = _SECRET.sub(lambda m: f"{m[1] or m[2]}***{'@' if m[1] else ''}", text)
    lowered = text.lower()
    for item in chain:
        code = getattr(item, "sqlstate", None)
        if code in _SQLSTATE_CAUSES:
            return _SQLSTATE_CAUSES[code], text[:300]
        if isinstance(item, socket.gaierror):
            return ConnectionCause.NAME_RESOLUTION, text[:300]
        if isinstance(item, ConnectionRefusedError):
            return ConnectionCause.CONNECTION_REFUSED, text[:300]
        if isinstance(item, TimeoutError):
            return ConnectionCause.TIMEOUT, text[:300]
    for needles, cause in _TEXT_CAUSES:
        if any(n in lowered for n in needles):
            return cause, text[:300]
    return ConnectionCause.OTHER, text[:300]
