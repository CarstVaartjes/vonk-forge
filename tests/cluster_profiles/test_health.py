from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
from jsonschema import Draft202012Validator

from cluster_profiles.backend import CommandResult
from cluster_profiles.fleet import Fleet, ManagementEndpoint, NodeId, NodeRecord
from cluster_profiles.health import LocalHealthError, NodeHealthService

ROOT = Path(__file__).resolve().parents[2]
RAW_SCHEMA = json.loads((ROOT / "schemas/node-health-raw.schema.json").read_text())
COUNTERS = tuple(RAW_SCHEMA["$defs"]["rdmaCounters"]["required"])


def raw_node(hostname: str) -> dict[str, object]:
    function = lambda interface, hca: {
        "interface": interface,
        "hca": hca,
        "operstate": "up",
        "carrier": 1,
        "speed_mbps": 200000,
        "mtu": 1500,
        "rdma_interface": interface,
        "rdma_state": "ACTIVE",
        "counters": {name: 0 for name in COUNTERS},
    }
    return {
        "schema_version": 1,
        "captured_at": "2026-08-02T12:00:00Z",
        "identity": {"hostname": hostname, "boot_id": "1" * 32, "uptime_seconds": 12345},
        "cpu": {"logical_processors": 20, "utilization_percent": 12.3, "load_1": 1.2, "load_5": 1.0, "load_15": 0.8},
        "memory": {"total_bytes": 130663231488, "available_bytes": 120000000000, "used_bytes": 10663231488, "used_percent": 8.2},
        "swap": {"total_bytes": 0, "free_bytes": 0, "used_bytes": 0, "used_percent": 0.0},
        "root_filesystem": {"total_bytes": 4031871553536, "available_bytes": 3787009835008, "used_bytes": 244861718528, "used_percent": 6.1, "read_only": False},
        "accelerator": {"available": True, "name": "NVIDIA GB10", "driver_version": "580.173.02", "utilization_percent": 0.0, "temperature_c": 40.0, "performance_state": "P8", "power_watts": None},
        "thermal_zones": [{"zone": "thermal_zone0", "type": "soc", "temperature_c": 40.0, "trip_points": []}],
        "fabric": {"functions": [function("enp1s0f1np1", "rocep1s0f1"), function("enP2p1s0f1np1", "roceP2p1s0f1")]},
        "services": {"docker_available": True, "docker_version": "29.2.1", "earlyoom_load_state": "not-found", "earlyoom_enabled": False, "earlyoom_active": False},
    }


INVENTORY = {
    "hosts": {
        node: {
            "hostname": hostname,
            "fabric": {
                "mtu": 1500,
                "link_rate_mbps": 200000,
                "function100": {"interface": "enp1s0f1np1", "hca": "rocep1s0f1", "gid_index": 3, "fabric_ip": "192.168.100.10" if node == "node1" else "192.168.100.11", "peer_ip": "192.168.100.11" if node == "node1" else "192.168.100.10"},
                "function101": {"interface": "enP2p1s0f1np1", "hca": "roceP2p1s0f1", "gid_index": 3, "fabric_ip": "192.168.101.10" if node == "node1" else "192.168.101.11", "peer_ip": "192.168.101.11" if node == "node1" else "192.168.101.10"},
            },
        }
        for node, hostname in (("node1", "node-3542"), ("node2", "node-2297"))
    }
}
BASELINE = {
    "schema_version": 2,
    "status": "passed",
    "evidence_scope": "live_runtime_verification",
    "inventory": "inventory/cluster.toml",
    "resolved_consumers": {
        "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_GID_INDEX": 3,
        "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
        "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
    },
    "rdma_counters_after": {
        f"{node}/{hca}/{counter}": 0
        for node in ("node1", "node2")
        for hca in ("rocep1s0f1", "roceP2p1s0f1")
        for counter in COUNTERS
    }
}


class FakeBackend:
    def __init__(self, raws: dict[str, dict[str, object]], *, barrier=None):
        self.raws = raws
        self.barrier = barrier
        self.results: dict[str, CommandResult] = {}
        self.calls = []

    def run_script(self, node, script, argv, timeout):
        self.calls.append((node, script, argv, timeout))
        if self.barrier:
            self.barrier.wait(timeout=1)
        if node in self.results:
            return self.results[node]
        return CommandResult(0, json.dumps(self.raws[node]).encode(), b"", False, False, False)


def service(*, barrier=None) -> tuple[NodeHealthService, FakeBackend, dict[str, dict[str, object]]]:
    raws = {"node1": raw_node("node-3542"), "node2": raw_node("node-2297")}
    backend = FakeBackend(raws, barrier=barrier)
    result = NodeHealthService(
        backend=backend,
        collector=b"#!/usr/bin/env bash\n",
        raw_schema=RAW_SCHEMA,
        result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()) if (ROOT / "schemas/node-health.schema.json").exists() else {},
        inventory=INVENTORY,
        rdma_baseline=BASELINE,
        timeout_seconds=10,
        cpu_sample_milliseconds=250,
    )
    return result, backend, raws


@pytest.mark.parametrize("count", [1, 3, 16])
def test_health_collects_configured_nodes_only(count: int) -> None:
    records = {}
    raws = {}
    endpoints = []
    baseline = {"rdma_counters_after": {}}
    for index in range(count):
        node_id = NodeId.parse(f"spk_{index:032x}")
        hostname = f"node-{index}.local"
        records[node_id] = NodeRecord(
            node_id, f"node-{index}", hostname,
            ManagementEndpoint(hostname, "operator"), {}, "ready",
        )
        raw = raw_node(hostname)
        if count == 1:
            raw["fabric"]["functions"] = []
        else:
            interface, hca = f"eth{index}", f"roce{index}"
            function = raw["fabric"]["functions"][0]
            function["interface"] = function["rdma_interface"] = interface
            function["hca"] = hca
            raw["fabric"]["functions"] = [function]
            endpoints.append({
                "node_id": node_id.value, "interface": interface, "hca": hca,
                "gid_index": 3, "mtu": 1500, "link_rate_mbps": 200000,
            })
            for counter in COUNTERS:
                baseline["rdma_counters_after"][f"{node_id.value}/{hca}/{counter}"] = 0
        raws[node_id.value] = raw
    topology = {
        "schema_version": 1,
        "nodes": [node_id.value for node_id in records],
        "links": [] if count == 1 else [{
            "id": "accepted-fabric", "kind": "switched-rdma",
            "accepted": True, "endpoints": endpoints,
        }],
    }
    backend = FakeBackend(raws)
    subject = NodeHealthService(
        backend=backend, collector=b"collector", raw_schema=RAW_SCHEMA,
        result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()),
        inventory={}, rdma_baseline=baseline, timeout_seconds=10,
        cpu_sample_milliseconds=250, fleet=Fleet(2, records), topology=topology,
        max_workers=4,
    )

    result = subject.collect()

    assert set(result.nodes) == {node_id.value for node_id in records}
    assert result.status == "healthy"
    assert {call[0] for call in backend.calls} == set(result.nodes)


def test_node_probes_overlap_and_results_are_canonical():
    barrier = threading.Barrier(2)
    subject, backend, _ = service(barrier=barrier)

    result = subject.collect()

    assert barrier.parties == 2
    assert tuple(result.nodes) == ("node1", "node2")
    assert {call[0] for call in backend.calls} == {"node1", "node2"}
    assert all(call[2][:4] == ("--json", "--cpu-sample-ms", "250", "--interface") for call in backend.calls)


def test_health_service_reuses_checked_schemas_and_bounds_malformed_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = {
        RAW_SCHEMA["$id"]: 0,
        json.loads((ROOT / "schemas/node-health.schema.json").read_text())["$id"]: 0,
    }
    check_schema = Draft202012Validator.check_schema

    def count_checked_schema(cls, schema: dict, *args: object, **kwargs: object) -> None:
        schemas[schema["$id"]] += 1
        check_schema(schema, *args, **kwargs)

    monkeypatch.setattr(
        Draft202012Validator, "check_schema", classmethod(count_checked_schema)
    )
    subject, _, raws = service()
    del raws["node1"]["schema_version"]

    for _ in range(2):
        node = subject.collect().nodes["node1"]
        assert node.status == "critical"
        assert node.errors == ("collector_malformed",)

    assert schemas == {
        "https://vonk-forge.local/schemas/node-health-raw.schema.json": 1,
        "https://vonk-forge.local/schemas/node-health.schema.json": 1,
    }


def test_generic_fleet_missing_topology_precedes_malformed_schema_validation() -> None:
    node_id = NodeId.parse("spk_00000000000000000000000000000001")
    fleet = Fleet(
        2,
        {
            node_id: NodeRecord(
                node_id,
                "node-1",
                "node-1.local",
                ManagementEndpoint("node-1.local", "operator"),
                {},
                "ready",
            )
        },
    )

    with pytest.raises(LocalHealthError, match="generic fleet health requires topology"):
        NodeHealthService(
            backend=FakeBackend({}),
            collector=b"collector",
            raw_schema={"type": 7},
            result_schema={"type": 7},
            inventory={},
            rdma_baseline={},
            timeout_seconds=10,
            cpu_sample_milliseconds=250,
            fleet=fleet,
        )


def test_generic_fleet_preserves_schema_error_boundary() -> None:
    node_id = NodeId.parse("spk_00000000000000000000000000000001")
    fleet = Fleet(
        2,
        {
            node_id: NodeRecord(
                node_id,
                "node-1",
                "node-1.local",
                ManagementEndpoint("node-1.local", "operator"),
                {},
                "ready",
            )
        },
    )
    topology = {
        "schema_version": 1,
        "nodes": [node_id.value],
        "links": [],
    }

    with pytest.raises(jsonschema.SchemaError):
        NodeHealthService(
            backend=FakeBackend({}),
            collector=b"collector",
            raw_schema={"type": 7},
            result_schema={"type": "object"},
            inventory={},
            rdma_baseline={},
            timeout_seconds=10,
            cpu_sample_milliseconds=250,
            fleet=fleet,
            topology=topology,
        )


@pytest.mark.parametrize("field", ("earlyoom_enabled", "earlyoom_active"))
def test_active_or_enabled_earlyoom_is_critical(field):
    subject, _, raws = service()
    raws["node2"]["services"][field] = True

    result = subject.collect()

    assert result.nodes["node2"].status == "critical"
    assert field in result.nodes["node2"].errors


def test_available_memory_is_not_a_generic_health_failure():
    subject, _, raws = service()
    raws["node1"]["memory"]["available_bytes"] = 1

    result = subject.collect()

    assert result.nodes["node1"].memory.available_bytes == 1
    assert "memory_low" not in result.nodes["node1"].errors


def test_rdma_counter_is_compared_with_accepted_absolute_baseline():
    subject, _, raws = service()
    subject.rdma_baseline["rdma_counters_after"]["node1/rocep1s0f1/packet_seq_err"] = 2
    raws["node1"]["fabric"]["functions"][0]["counters"]["packet_seq_err"] = 3

    result = subject.collect()

    assert "rdma_counter_above_baseline" in result.nodes["node1"].errors


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        (lambda raw: raw["root_filesystem"].__setitem__("read_only", True), "root_filesystem_read_only"),
        (lambda raw: raw["accelerator"].__setitem__("available", False), "nvidia_unavailable"),
        (lambda raw: raw["fabric"]["functions"][0].__setitem__("speed_mbps", 100000), "fabric_mismatch"),
        (lambda raw: raw["fabric"]["functions"][0].__setitem__("mtu", 9000), "fabric_mismatch"),
        (lambda raw: raw["fabric"]["functions"][0].__setitem__("hca", "wrong"), "fabric_mismatch"),
        (lambda raw: raw["fabric"]["functions"][0].__setitem__("rdma_state", "DOWN"), "rdma_inactive"),
    ),
)
def test_foundation_failures_are_critical(mutation, code):
    subject, _, raws = service()
    mutation(raws["node1"])

    node = subject.collect().nodes["node1"]

    assert node.status == "critical"
    assert code in node.errors


def test_thermal_critical_wins_and_warning_trips_are_stable_sorted():
    subject, _, raws = service()
    raws["node1"]["thermal_zones"] = [{
        "zone": "thermal_zone0", "type": "soc", "temperature_c": 90,
        "trip_points": [
            {"type": "hot", "temperature_c": 80, "reached": True},
            {"type": "critical", "temperature_c": 85, "reached": True},
        ],
    }]
    raws["node1"]["services"]["earlyoom_active"] = True

    node = subject.collect().nodes["node1"]

    assert node.status == "critical"
    assert node.errors == tuple(sorted(node.errors))
    assert "thermal_critical_trip" in node.errors
    assert "thermal_trip" in node.warnings


def test_capacity_and_wholly_missing_optional_sources_warn_only():
    subject, _, raws = service()
    raw = raws["node1"]
    raw["swap"]["used_bytes"] = 1073741825
    raw["root_filesystem"]["available_bytes"] = 150 * 1024**3 - 1
    raw["services"]["docker_available"] = False
    raw["services"]["docker_version"] = None
    raw["thermal_zones"] = []

    node = subject.collect().nodes["node1"]

    assert node.status == "warning"
    assert node.warnings == tuple(sorted(("docker_unavailable", "root_free_low", "swap_used_high", "thermal_unavailable")))


def test_individual_optional_accelerator_fields_remain_null_without_warning():
    subject, _, raws = service()
    raw = raws["node1"]["accelerator"]
    raw["power_watts"] = None
    raw["temperature_c"] = None

    node = subject.collect().nodes["node1"]

    assert node.status == "healthy"
    assert node.accelerator.power_watts is None


@pytest.mark.parametrize(
    ("result", "code"),
    (
        (CommandResult(None, b"", b"", True, False, False), "collector_timeout"),
        (CommandResult(1, b"{}", b"", False, False, False), "collector_nonzero"),
        (CommandResult(0, b"{}", b"", False, True, False), "collector_truncated"),
        (CommandResult(0, b"{", b"", False, False, False), "collector_malformed"),
        (CommandResult(0, b"\xff", b"", False, False, False), "collector_malformed"),
    ),
)
def test_remote_collector_failures_are_bounded_critical_results(result, code):
    subject, backend, _ = service()
    backend.results["node1"] = result

    node = subject.collect().nodes["node1"]

    assert node.status == "critical"
    assert node.errors == (code,)


def test_ssh_connection_failure_is_unreachable_and_preserves_other_node():
    subject, backend, _ = service()
    backend.results["node2"] = CommandResult(255, b"", b"permission denied", False, False, False)

    result = subject.collect()

    assert result.status == "critical"
    assert result.nodes["node1"].status == "healthy"
    assert result.nodes["node2"].status == "unreachable"
    assert result.nodes["node2"].to_dict()["identity"] is None


def test_result_validates_schema_and_rounds_percentages():
    subject, _, raws = service()
    raws["node1"]["cpu"]["utilization_percent"] = 12.345

    document = subject.collect().to_dict()

    jsonschema.validate(document, json.loads((ROOT / "schemas/node-health.schema.json").read_text()))
    assert document["nodes"]["node1"]["cpu"]["utilization_percent"] == 12.3


def test_hostname_mismatch_is_critical():
    subject, _, raws = service()
    raws["node1"]["identity"]["hostname"] = "node-2297"

    node = subject.collect().nodes["node1"]

    assert node.status == "critical"
    assert "hostname_mismatch" in node.errors


@pytest.mark.parametrize(
    ("path", "code"),
    (
        (("root_filesystem", "read_only"), "root_filesystem_unavailable"),
        (("fabric", "functions", 0, "carrier"), "fabric_mismatch"),
        (("fabric", "functions", 0, "counters", "packet_seq_err"), "rdma_counter_unavailable"),
    ),
)
def test_unknown_critical_foundation_fields_fail_closed(path, code):
    subject, _, raws = service()
    value = raws["node1"]
    for part in path[:-1]:
        value = value[part]
    value[path[-1]] = None

    node = subject.collect().nodes["node1"]

    assert node.status == "critical"
    assert code in node.errors


def test_missing_baseline_entry_fails_before_probe():
    backend = FakeBackend({"node1": raw_node("node-3542"), "node2": raw_node("node-2297")})
    baseline = deepcopy(BASELINE)
    del baseline["rdma_counters_after"]["node1/rocep1s0f1/packet_seq_err"]

    with pytest.raises(LocalHealthError, match="missing RDMA baseline"):
        NodeHealthService(
            backend=backend, collector=b"x", raw_schema=RAW_SCHEMA,
            result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()),
            inventory=INVENTORY, rdma_baseline=baseline,
            timeout_seconds=10, cpu_sample_milliseconds=250,
        )

    assert backend.calls == []


def test_cross_node_gid_drift_fails_locally_but_gid_is_not_remote_argv():
    inventory = deepcopy(INVENTORY)
    inventory["hosts"]["node2"]["fabric"]["function100"]["gid_index"] = 4
    backend = FakeBackend({})

    with pytest.raises(LocalHealthError, match="accepted GID index"):
        NodeHealthService(
            backend=backend, collector=b"x", raw_schema=RAW_SCHEMA,
            result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()),
            inventory=inventory, rdma_baseline=BASELINE,
            timeout_seconds=10, cpu_sample_milliseconds=250,
        )

    subject, backend, _ = service()
    subject.collect()
    assert all("3" not in call[2] for call in backend.calls)


def test_inventory_gid_must_match_accepted_baseline_consumers():
    inventory = deepcopy(INVENTORY)
    for node in ("node1", "node2"):
        for name in ("function100", "function101"):
            inventory["hosts"][node]["fabric"][name]["gid_index"] = 4

    with pytest.raises(LocalHealthError, match="accepted GID index"):
        NodeHealthService(
            backend=FakeBackend({}), collector=b"x", raw_schema=RAW_SCHEMA,
            result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()),
            inventory=inventory, rdma_baseline=BASELINE,
            timeout_seconds=10, cpu_sample_milliseconds=250,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("mtu", 9000), ("link_rate_mbps", 100000)),
)
def test_symmetric_inventory_link_drift_is_rejected(field, value):
    inventory = deepcopy(INVENTORY)
    for node in ("node1", "node2"):
        inventory["hosts"][node]["fabric"][field] = value

    with pytest.raises(LocalHealthError, match="approved MTU and link rate"):
        NodeHealthService(
            backend=FakeBackend({}), collector=b"x", raw_schema=RAW_SCHEMA,
            result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()),
            inventory=inventory, rdma_baseline=BASELINE,
            timeout_seconds=10, cpu_sample_milliseconds=250,
        )


def test_symmetric_function101_gid_drift_is_rejected():
    inventory = deepcopy(INVENTORY)
    for node in ("node1", "node2"):
        inventory["hosts"][node]["fabric"]["function101"]["gid_index"] = 4

    with pytest.raises(LocalHealthError, match="accepted GID index"):
        NodeHealthService(
            backend=FakeBackend({}), collector=b"x", raw_schema=RAW_SCHEMA,
            result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()),
            inventory=inventory, rdma_baseline=BASELINE,
            timeout_seconds=10, cpu_sample_milliseconds=250,
        )


def test_malformed_toml_shapes_are_bounded_local_health_errors():
    inventory = {"hosts": ["node1", "node2"]}

    with pytest.raises(LocalHealthError, match="invalid health inventory"):
        NodeHealthService(
            backend=FakeBackend({}), collector=b"x", raw_schema=RAW_SCHEMA,
            result_schema=json.loads((ROOT / "schemas/node-health.schema.json").read_text()),
            inventory=inventory, rdma_baseline=BASELINE,
            timeout_seconds=10, cpu_sample_milliseconds=250,
        )


def test_output_schema_enforces_unreachable_null_envelope():
    subject, _, _ = service()
    document = subject.collect().to_dict()
    document["nodes"]["node1"]["status"] = "unreachable"
    document["nodes"]["node1"]["errors"] = ["ssh_unreachable"]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, json.loads((ROOT / "schemas/node-health.schema.json").read_text()))


def test_root_and_packaged_output_schemas_match():
    assert (ROOT / "schemas/node-health.schema.json").read_bytes() == (
        ROOT / "src/cluster_profiles/schemas/node-health.schema.json"
    ).read_bytes()


def test_local_assets_fail_before_any_probe(tmp_path):
    backend = FakeBackend({})

    with pytest.raises(LocalHealthError, match="local health assets"):
        NodeHealthService.from_repository(tmp_path, backend)

    assert backend.calls == []
