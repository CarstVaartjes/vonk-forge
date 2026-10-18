"""Exception types that prove an API dependency is unavailable.

Programming/validation errors and SQL statement/integrity errors are deliberately
absent: repeating the same request cannot repair them.
"""

from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

AVAILABILITY_ERRORS = (
    OSError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
    PoolTimeoutError,
)
