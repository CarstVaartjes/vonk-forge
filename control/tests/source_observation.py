"""Bounded, stateless observation of stored audit inputs."""

from collections.abc import Callable
from time import sleep

from vonk_agent_protocol import UnknownOutcomeError, WaitReason


def observe[Result](read: Callable[[], Result]) -> Result:
    """Publish only a complete observation; each invocation owns a fresh budget."""
    for attempt in range(2):
        try:
            return read()
        except (
            OSError,
            UnicodeError,
            ValueError,
            SyntaxError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            sleep(0.01 * (attempt + 1))
    try:
        return read()
    except (
        OSError,
        UnicodeError,
        ValueError,
        SyntaxError,
        TypeError,
        KeyError,
        AttributeError,
    ) as error:
        raise UnknownOutcomeError(
            str(error), reason=WaitReason.OBSERVATION_UNAVAILABLE
        ) from error
