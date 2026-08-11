import json
from pathlib import Path

from cluster_profiles.fleet.loaders import (
    load_fleet,
    validate_topology_references,
)

ROOT = Path(__file__).resolve().parents[3]
EXPECTED_NODES = {
    "spk_42a502cc1a5de4c79aea1b6b6d993c74",
    "spk_ec7897d93866091c4249cc7825fb95c7",
}


def test_repository_inventory_records_the_accepted_physical_fleet() -> None:
    fleet = load_fleet(ROOT / "inventory/fleet.toml")
    assert {node_id.value for node_id in fleet.nodes} == EXPECTED_NODES
    assert all(node.lifecycle == "ready" for node in fleet.nodes.values())

    topology = json.loads((ROOT / "inventory/topology.json").read_text())
    validate_topology_references(topology)
    assert set(topology["nodes"]) == EXPECTED_NODES
    assert all(link["accepted"] is True for link in topology["links"])
