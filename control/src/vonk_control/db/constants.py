"""Db: constants."""

import logging
from pathlib import Path

from sqlalchemy.exc import (
    InterfaceError,
    OperationalError,
    TimeoutError,
)

_STARTUP_ADVISORY_LOCK = 8_241_779_103


_MODULE_ALEMBIC_CONFIG = Path(__file__).resolve().parents[1] / "alembic.ini"


_SOURCE_ALEMBIC_CONFIG = Path(__file__).resolve().parents[3] / "alembic.ini"


_ALEMBIC_CONFIG = (
    _MODULE_ALEMBIC_CONFIG
    if _MODULE_ALEMBIC_CONFIG.is_file()
    else _SOURCE_ALEMBIC_CONFIG
)


_DATABASE_STARTUP_TIMEOUT_SECONDS = 120.0


_DATABASE_RETRYABLE_ERRORS = (InterfaceError, OperationalError, TimeoutError)


_LOGGER = logging.getLogger(__package__)
