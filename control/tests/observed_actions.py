"""Observe a completed call; behavioral tests inspect its effects, not its error taxonomy."""

from collections.abc import Callable


def observe_action[T](action: Callable[[], T]) -> T | Exception:
    try:
        return action()
    except Exception as outcome:  # noqa: BLE001
        return outcome
