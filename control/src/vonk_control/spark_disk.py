"""The free disk a Spark can offer a claim.

A Spark's agent removes its unused model objects (model files no
installation links or names), least recently used first, before a claim
writes (``store_retention::make_room``). Admission and the storage collector
therefore count them as space the Spark frees itself: an installation is
uninstalled, or work refused, only for a need beyond free plus those bytes.
An agent that does not report them (older) is judged on free alone.
"""

from __future__ import annotations


def spark_free_bytes(
    free: int, unused_model_bytes: int | None, *, reused_model_bytes: int = 0
) -> int:
    """``free`` plus the Spark's unused model bytes, less any of them the
    claim itself reuses (the agent never removes a claim's own objects)."""

    return free + max(0, (unused_model_bytes or 0) - reused_model_bytes)
