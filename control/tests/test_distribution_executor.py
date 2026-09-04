from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from vonk_control.distribution_executor import DurableDistributionPhaseExecutor


def _target(node: str, *, image: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=node,
        state="ready",
        verified_sha256=("e" * 64 if image else "b" * 64),
        imported_image_digest=("sha256:" + "d" * 64 if image else None),
    )


def test_complete_two_node_distribution_is_a_verified_skip() -> None:
    nodes = ("spk_" + "a" * 32, "spk_" + "b" * 32)
    preparation = SimpleNamespace(
        model=SimpleNamespace(artifact_set_sha256="b" * 64, targets=[_target(node) for node in nodes]),
        runtime_image=SimpleNamespace(
            image_digest="sha256:" + "d" * 64,
            oci_layout_sha256="e" * 64,
            targets=[_target(node, image=True) for node in nodes],
        ),
    )
    plan = SimpleNamespace(
        preparation=preparation,
        storage=SimpleNamespace(artifact_digests=["c" * 64]),
        image_digest="sha256:" + "d" * 64,
        build=SimpleNamespace(oci_layout_sha256="e" * 64, image_bytes=11),
        recipe_build_id=None,
    )
    phase = SimpleNamespace(kind="transfer", node_ids=list(nodes), index=0)
    executor = DurableDistributionPhaseExecutor(None, None, None, clock=lambda: datetime.now(UTC))
    result = executor.execute(plan, phase, item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress={})
    assert result.operation_id is None
    assert result.result == {"skipped": True, "verified": False, "verified_digests": ["c" * 64], "verified_image_digest": "sha256:" + "d" * 64, "verified_oci_layout_sha256": "e" * 64, "cached_nodes": list(nodes)}
