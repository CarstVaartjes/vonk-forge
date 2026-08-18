from __future__ import annotations

import pytest
from vonk_control.auth import Actor
from vonk_control.library_operations import (
    LibraryLifecycleAuthority,
    LibraryOperationDenied,
    LibraryPlacementConflict,
    SparkCapacity,
)


def test_preview_selects_eligible_sparks_and_binds_exact_digest() -> None:
    preview = LibraryLifecycleAuthority.preview_placement(
        {"name": "pair", "node_count": 2},
        (
            SparkCapacity("spk_" + "a" * 32, 100, 100, ("gpu",)),
            SparkCapacity("spk_" + "b" * 32, 100, 100, ("gpu",)),
            SparkCapacity("spk_" + "c" * 32, 1, 1, ("gpu",)),
        ),
        required_bytes=50,
        required_memory_bytes=50,
        required_capabilities=("gpu",),
    )
    assert preview.nodes == ("spk_" + "a" * 32, "spk_" + "b" * 32)
    assert preview.rejected["spk_" + "c" * 32] == "insufficient capacity"
    LibraryLifecycleAuthority.confirm(preview, preview.placement_digest)
    with pytest.raises(LibraryPlacementConflict, match="stale"):
        LibraryLifecycleAuthority.confirm(preview, "0" * 64)


def test_preview_rejects_insufficient_eligible_sparks() -> None:
    with pytest.raises(LibraryPlacementConflict, match="fewer eligible"):
        LibraryLifecycleAuthority.preview_placement(
            {"name": "pair", "node_count": 2},
            (SparkCapacity("spk_" + "a" * 32, 1, 1),),
            required_bytes=2,
            required_memory_bytes=2,
        )


def test_mutation_requires_library_operate_unless_administrator() -> None:
    with pytest.raises(LibraryOperationDenied):
        LibraryLifecycleAuthority.require_operate(Actor("operator", "operator"), ())
    LibraryLifecycleAuthority.require_operate(
        Actor("operator", "operator"), ("library:operate",)
    )
    LibraryLifecycleAuthority.require_operate(Actor("admin", "administrator"), ())
