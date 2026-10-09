"""Resource planning: memory kinds."""

from __future__ import annotations

from vonk_agent_protocol.inventory import MemoryPool


def memory_reservation_kind(kind: str) -> str:
    return {
        "unified": "unified-memory",
        "host": "host-memory",
        "accelerator": "gpu-memory",
    }[kind]


def memory_reservation_kinds(kind: str, pool: MemoryPool) -> tuple[str, ...]:
    """Declared consumers of one physical pool, independent of owner names."""
    kinds = ("host-memory", "gpu-memory", "unified-memory")
    if kind not in kinds:
        raise ValueError("reservation is not memory")
    if pool == "shared":
        return kinds
    if kind == "unified-memory":
        # Unified demand is checked against both independent capacities below.
        return kinds
    return kind, "unified-memory"
