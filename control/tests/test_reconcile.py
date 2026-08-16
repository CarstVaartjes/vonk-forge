from vonk_control.orchestration import OperationGraph
from vonk_control.reconcile import resolved_reconciliation_plan


def _graph() -> OperationGraph:
    return OperationGraph("pending", "a" * 40, (), (), "f" * 64)


def test_resolved_reconciliation_plan_is_digest_bound_and_canonical() -> None:
    first = resolved_reconciliation_plan(
        commit="a" * 40,
        targets=("spk_b", "spk_a"),
        placements={"chat": ("spk_a", "spk_b")},
        routes={"chat": {"entrypoint_node_id": "spk_a"}},
        releases={"chat": "sha256:" + "b" * 64},
        workload_groups={},
        input_digests={"fleet": "f" * 64},
        operation_graph=_graph(),
        operation_payloads={},
        agent_protocol_range=(1, 1),
    )
    second = resolved_reconciliation_plan(
        commit="a" * 40,
        targets=("spk_a", "spk_b"),
        placements={"chat": ("spk_a", "spk_b")},
        routes={"chat": {"entrypoint_node_id": "spk_a"}},
        releases={"chat": "sha256:" + "b" * 64},
        workload_groups={},
        input_digests={"fleet": "f" * 64},
        operation_graph=_graph(),
        operation_payloads={},
        agent_protocol_range=(1, 1),
    )

    assert first == second
    assert first.targets == ("spk_a", "spk_b")
    assert len(first.digest) == 64


def test_resolved_reconciliation_plan_rejects_duplicate_or_empty_targets() -> None:
    for targets in ((), ("spk_a", "spk_a"), ("",)):
        try:
            resolved_reconciliation_plan(
                commit="a" * 40,
                targets=targets,
                placements={},
                routes={},
                releases={},
                workload_groups={},
                input_digests={},
                operation_graph=_graph(),
                operation_payloads={},
                agent_protocol_range=(1, 1),
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid targets: {targets!r}")
