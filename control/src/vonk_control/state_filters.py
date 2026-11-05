"""Selecting rows by a state a caller named, in the core vocabulary.

A caller (an API filter, a CLI argument) names a state with a word of the core
vocabulary; any other word matches only rows that carry exactly that word.
"""

from __future__ import annotations

from typing import Any

from vonk_agent_protocol import LifecycleSubject, input_state, stored_words


def state_filter(column: Any, subject: LifecycleSubject, word: str) -> Any:
    """``column`` holds a word that means the state ``word`` names."""

    named = input_state(word)
    words = stored_words(subject, [named]) if named is not None else (word,)
    return column.in_(words)
