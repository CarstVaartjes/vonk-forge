"""One way to shorten a failure summary: at a whole word, never inside one."""

from __future__ import annotations


def summary_cut(text: str, limit: int = 256) -> str:
    """The text itself, or its start ending at a whole word within `limit` (detail keeps the full text)."""

    if len(text) <= limit:
        return text
    head = text[:limit]
    # A cut that lands exactly between words keeps the whole last word.
    if text[limit].isspace():
        return head.rstrip()
    words = head.rsplit(None, 1)
    return words[0] if len(words) == 2 else head
