import pytest
from vonk_control.topology import Placement, TopologyError, validate_topology

from tests.canonical_recipe_fixtures import topology_document


def multinode():
    document = topology_document()
    document["topology"] = {
        "name": "triple-tp3",
        "node_count": 3,
        "roles": [
            document["topology"]["roles"][0],
            {
                **document["topology"]["roles"][0],
                "name": "worker",
                "count": 2,
                "endpoint_owner": False,
            },
        ],
        "parallelism": {
            "tensor": 3,
            "pipeline": 1,
            "data": 1,
            "backend": "tcp",
        },
        "start_order": ["worker", "entrypoint"],
    }
    return document


def placements():
    return (
        Placement("spk_" + "1" * 32, 1, "worker", False),
        Placement("spk_" + "2" * 32, 0, "entrypoint", True),
        Placement("spk_" + "3" * 32, 2, "worker", False),
    )


def capabilities(values):
    return {
        item.node_id: ("runtime.vonk.v1", "fabric.full_mesh.mbps.200000")
        for item in values
    }


def test_three_node_topology_has_deterministic_ranks() -> None:
    values = placements()

    result = validate_topology(multinode(), values, capabilities(values))

    assert [item.rank for item in result] == [0, 1, 2]
    assert result[0].role == "entrypoint"


@pytest.mark.parametrize(
    "values",
    [
        (
            Placement("spk_" + "1" * 32, 0, "entrypoint"),
            Placement("spk_" + "2" * 32, 0, "worker"),
            Placement("spk_" + "3" * 32, 2, "worker"),
        ),
        (
            Placement("spk_" + "1" * 32, 0, "entrypoint"),
            Placement("spk_" + "1" * 32, 1, "worker"),
            Placement("spk_" + "3" * 32, 2, "worker"),
        ),
        (
            Placement("spk_" + "1" * 32, 0, "worker"),
            Placement("spk_" + "2" * 32, 1, "worker"),
            Placement("spk_" + "3" * 32, 2, "worker"),
        ),
    ],
)
def test_invalid_rank_node_and_role_shapes_are_blocked(values) -> None:
    with pytest.raises(TopologyError):
        validate_topology(multinode(), values, capabilities(values))


def test_missing_runtime_or_fabric_capability_is_blocking() -> None:
    values = placements()
    with pytest.raises(TopologyError) as caught:
        validate_topology(
            multinode(),
            values,
            {
                item.node_id: ("runtime.sglang.v1", "fabric.full_mesh.mbps.200000")
                for item in values
            },
        )
    assert caught.value.code == "topology.runtime_capability_missing"

    with pytest.raises(TopologyError) as caught:
        validate_topology(
            multinode(),
            values,
            {
                item.node_id: ("runtime.vonk.v1", "fabric.connected.mbps.100000")
                for item in values
            },
        )
    assert caught.value.code == "topology.fabric_insufficient"


def test_role_identity_is_bound_to_each_rank() -> None:
    values = (
        Placement("spk_" + "1" * 32, 0, "worker"),
        Placement("spk_" + "2" * 32, 1, "worker"),
        Placement("spk_" + "3" * 32, 2, "entrypoint"),
    )

    with pytest.raises(TopologyError) as caught:
        validate_topology(multinode(), values, capabilities(values))

    assert caught.value.code == "topology.role_mismatch"


def test_multiple_endpoint_owners_are_rejected() -> None:
    values = (
        Placement("spk_" + "1" * 32, 0, "entrypoint", True),
        Placement("spk_" + "2" * 32, 1, "worker", True),
        Placement("spk_" + "3" * 32, 2, "worker", False),
    )

    with pytest.raises(TopologyError) as caught:
        validate_topology(multinode(), values, capabilities(values))

    assert caught.value.code == "topology.role_mismatch"
