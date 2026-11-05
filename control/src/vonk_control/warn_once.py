"""Log a recurring condition once per process, with a bounded memory of keys."""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)
_SEEN: set[str] = set()
_LIMIT = 4096


def warn_once(
    key: str,
    message: str,
    *args: object,
    exc_info: BaseException | None = None,
) -> None:
    """Warn the first time ``key`` is seen; past the bound, warn every time."""

    if key in _SEEN:
        return
    if len(_SEEN) < _LIMIT:
        _SEEN.add(key)
    _LOGGER.warning(message, *args, exc_info=exc_info)
