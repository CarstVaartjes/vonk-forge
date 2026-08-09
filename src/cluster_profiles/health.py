"""Concurrent, read-only health evaluation for configured Vonk Forge GPU nodes."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import ValidationError, validators

from .backend import CommandResult, SshBackend
from .fleet import Fleet
from .fleet.loaders import validate_topology_references

_NODES = ("node1", "node2")
_SWAP_WARNING_BYTES = 1024**3
_ROOT_FREE_WARNING_BYTES = 150 * 1024**3
_APPROVED_FABRIC_MTU = 1500
_APPROVED_LINK_RATE_MBPS = 200000
_APPROVED_GID_INDEX = 3
_DEVICE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class LocalHealthError(ValueError):
    """A checked-in local health asset could not be loaded safely."""


class Telemetry(Mapping[str, Any]):
    """Small immutable mapping with attribute access for telemetry fields."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = MappingProxyType(
            {
                str(key): self._wrap(value)
                for key, value in values.items()
            }
        )

    @classmethod
    def _wrap(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return cls(value)
        if isinstance(value, list):
            return tuple(cls._wrap(item) for item in value)
        return value

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def to_dict(self) -> dict[str, Any]:
        def unwrap(value: Any) -> Any:
            if isinstance(value, Telemetry):
                return value.to_dict()
            if isinstance(value, tuple):
                return [unwrap(item) for item in value]
            return value

        return {
            key: unwrap(value)
            for key, value in self._values.items()
        }


@dataclass(frozen=True)
class NodeHealth:
    status: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    identity: Telemetry | None
    cpu: Telemetry | None
    memory: Telemetry | None
    swap: Telemetry | None
    root_filesystem: Telemetry | None
    accelerator: Telemetry | None
    thermal_zones: tuple[Telemetry, ...]
    fabric: Telemetry | None
    services: Telemetry | None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> NodeHealth:
        section = lambda name: (
            Telemetry(value[name]) if isinstance(value.get(name), Mapping) else None
        )
        return cls(
            status=str(value["status"]),
            errors=tuple(sorted(str(item) for item in value.get("errors", ()))),
            warnings=tuple(sorted(str(item) for item in value.get("warnings", ()))),
            identity=section("identity"),
            cpu=section("cpu"),
            memory=section("memory"),
            swap=section("swap"),
            root_filesystem=section("root_filesystem"),
            accelerator=section("accelerator"),
            thermal_zones=tuple(
                Telemetry(item) for item in value.get("thermal_zones", ())
            ),
            fabric=section("fabric"),
            services=section("services"),
        )

    def to_dict(self) -> dict[str, Any]:
        def section(value: Telemetry | None) -> dict[str, Any] | None:
            return value.to_dict() if value is not None else None

        return {
            "status": self.status,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "identity": section(self.identity),
            "cpu": section(self.cpu),
            "memory": section(self.memory),
            "swap": section(self.swap),
            "root_filesystem": section(self.root_filesystem),
            "accelerator": section(self.accelerator),
            "thermal_zones": [zone.to_dict() for zone in self.thermal_zones],
            "fabric": section(self.fabric),
            "services": section(self.services),
        }


@dataclass(frozen=True)
class ClusterHealth:
    schema_version: int
    captured_at: str
    status: str
    nodes: Mapping[str, NodeHealth]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "status": self.status,
            "nodes": {node: self.nodes[node].to_dict() for node in sorted(self.nodes)},
        }


class NodeHealthService:
    """Probe both nodes concurrently and grade only approved foundation rules."""

    def __init__(
        self,
        *,
        backend: SshBackend,
        collector: bytes,
        raw_schema: Mapping[str, Any],
        result_schema: Mapping[str, Any],
        inventory: Mapping[str, Any],
        rdma_baseline: Mapping[str, Any],
        timeout_seconds: float,
        cpu_sample_milliseconds: int,
        fleet: Fleet | None = None,
        topology: Mapping[str, Any] | None = None,
        max_workers: int = 8,
    ) -> None:
        if not collector or timeout_seconds <= 0 or cpu_sample_milliseconds <= 0 or max_workers <= 0:
            raise LocalHealthError("invalid collector or health timing configuration")
        if fleet is not None and topology is None:
            raise LocalHealthError("generic fleet health requires topology")
        self.backend = backend
        self.collector = collector
        self.raw_schema = dict(raw_schema)
        self.result_schema = dict(result_schema)
        if fleet is None:
            try:
                raw_validator_class = validators.validator_for(self.raw_schema)
                raw_validator_class.check_schema(self.raw_schema)
                result_validator_class = validators.validator_for(self.result_schema)
                result_validator_class.check_schema(self.result_schema)
            except Exception as error:
                raise LocalHealthError(f"invalid node health schema: {error}") from error
        else:
            raw_validator_class = validators.validator_for(self.raw_schema)
            raw_validator_class.check_schema(self.raw_schema)
            result_validator_class = validators.validator_for(self.result_schema)
            result_validator_class.check_schema(self.result_schema)
        self._raw_validator = raw_validator_class(self.raw_schema)
        self._result_validator = result_validator_class(self.result_schema)
        self.inventory = inventory
        self.rdma_baseline = rdma_baseline
        self.timeout_seconds = timeout_seconds
        self.cpu_sample_milliseconds = cpu_sample_milliseconds
        self.max_workers = max_workers
        self._nodes = tuple(
            node_id.value
            for node_id, record in sorted(fleet.nodes.items() if fleet else ())
            if record.lifecycle != "retired"
        ) or _NODES
        if fleet is None:
            self._functions = self._validate_local_assets()
        else:
            self._functions = self._validate_generic_assets(fleet, topology)

    @classmethod
    def from_repository(
        cls,
        root: Path,
        backend: SshBackend,
        *,
        fleet: Fleet | None = None,
        topology: Mapping[str, Any] | None = None,
    ) -> NodeHealthService:
        root = root.resolve()
        try:
            with (root / "config/controller.toml").open("rb") as source:
                controller = tomllib.load(source)
            health = controller["health"]
            paths = {
                name: cls._safe_path(root, health[name])
                for name in ("collector", "inventory", "rdma_baseline")
            }
            collector = paths["collector"].read_bytes()
            with paths["inventory"].open("rb") as source:
                inventory = tomllib.load(source)
            rdma_baseline = json.loads(paths["rdma_baseline"].read_text())
            raw_schema = json.loads((root / "schemas/node-health-raw.schema.json").read_text())
            result_schema = json.loads((root / "schemas/node-health.schema.json").read_text())
            return cls(
                backend=backend,
                collector=collector,
                raw_schema=raw_schema,
                result_schema=result_schema,
                inventory=inventory,
                rdma_baseline=rdma_baseline,
                timeout_seconds=float(health["timeout_seconds"]),
                cpu_sample_milliseconds=int(health["cpu_sample_milliseconds"]),
                fleet=fleet,
                topology=topology,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
            if isinstance(error, LocalHealthError):
                raise
            raise LocalHealthError(f"cannot load local health assets: {error}") from error

    @staticmethod
    def _safe_path(root: Path, configured: object) -> Path:
        path = Path(str(configured))
        if path.is_absolute() or ".." in path.parts:
            raise LocalHealthError("health asset path must stay within repository")
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise LocalHealthError("health asset path must stay within repository")
        return resolved

    def _validate_local_assets(self) -> Mapping[str, tuple[tuple[str, str], ...]]:
        try:
            hosts = self.inventory["hosts"]
            baseline = self.rdma_baseline["rdma_counters_after"]
            if (
                tuple(sorted(hosts)) != _NODES
                or self.rdma_baseline.get("schema_version") != 2
                or self.rdma_baseline.get("status") != "passed"
                or self.rdma_baseline.get("evidence_scope") != "live_runtime_verification"
                or self.rdma_baseline.get("inventory") != "inventory/cluster.toml"
                or not isinstance(baseline, Mapping)
            ):
                raise TypeError("inventory must define exactly node1 and node2")
            counter_names = tuple(self.raw_schema["$defs"]["rdmaCounters"]["required"])
            functions: dict[str, tuple[tuple[str, str], ...]] = {}
            for node in _NODES:
                if not isinstance(hosts[node].get("hostname"), str) or not hosts[node]["hostname"]:
                    raise TypeError(f"{node} hostname must be a nonempty string")
                fabric = hosts[node]["fabric"]
                if (
                    not isinstance(fabric.get("mtu"), int)
                    or isinstance(fabric.get("mtu"), bool)
                    or fabric["mtu"] != _APPROVED_FABRIC_MTU
                    or not isinstance(fabric.get("link_rate_mbps"), int)
                    or isinstance(fabric.get("link_rate_mbps"), bool)
                    or fabric["link_rate_mbps"] != _APPROVED_LINK_RATE_MBPS
                ):
                    raise TypeError(
                        f"{node} fabric must use approved MTU and link rate"
                    )
                function_names = tuple(
                    name for name in sorted(fabric) if name.startswith("function")
                )
                if function_names != ("function100", "function101"):
                    raise TypeError(f"{node} must define function100 and function101")
                records = tuple(
                    (fabric[name]["interface"], fabric[name]["hca"])
                    for name in function_names
                )
                if len(records) != 2 or len(set(records)) != 2:
                    raise TypeError(f"{node} must define two unique fabric functions")
                for interface, hca in records:
                    if (
                        not isinstance(interface, str)
                        or not isinstance(hca, str)
                        or _DEVICE_NAME.fullmatch(interface) is None
                        or _DEVICE_NAME.fullmatch(hca) is None
                    ):
                        raise TypeError("fabric names must be safe device strings")
                    for counter in counter_names:
                        value = baseline.get(f"{node}/{hca}/{counter}")
                        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                            raise TypeError(
                                f"missing RDMA baseline for {node}/{hca}/{counter}"
                            )
                functions[node] = records
                for name in function_names:
                    item = fabric[name]
                    if (
                        not isinstance(item.get("gid_index"), int)
                        or isinstance(item.get("gid_index"), bool)
                        or item["gid_index"] != _APPROVED_GID_INDEX
                        or not isinstance(item.get("fabric_ip"), str)
                        or not isinstance(item.get("peer_ip"), str)
                    ):
                        raise TypeError(
                            f"invalid {node} {name} accepted GID index or address pin"
                        )
            for name in ("function100", "function101"):
                left = hosts["node1"]["fabric"][name]
                right = hosts["node2"]["fabric"][name]
                if (
                    left["interface"] != right["interface"]
                    or left["hca"] != right["hca"]
                    or left["gid_index"] != right["gid_index"]
                    or left["fabric_ip"] != right["peer_ip"]
                    or left["peer_ip"] != right["fabric_ip"]
                ):
                    raise TypeError(f"cross-node fabric pins differ for {name}")
            interfaces = ",".join(interface for interface, _ in functions["node1"])
            hcas = ",".join(f"{hca}:1" for _, hca in functions["node1"])
            expected_consumers = {
                "GLOO_SOCKET_IFNAME": interfaces,
                "NCCL_IB_GID_INDEX": _APPROVED_GID_INDEX,
                "NCCL_IB_HCA": f"={hcas}",
                "NCCL_SOCKET_IFNAME": f"={interfaces}",
                "TP_SOCKET_IFNAME": interfaces,
            }
            if self.rdma_baseline.get("resolved_consumers") != expected_consumers:
                raise TypeError("inventory does not match accepted RDMA consumers")
            return MappingProxyType(functions)
        except (AttributeError, IndexError, KeyError, TypeError) as error:
            raise LocalHealthError(f"invalid health inventory or RDMA baseline: {error}") from error

    def _validate_generic_assets(
        self, fleet: Fleet, topology: Mapping[str, Any]
    ) -> Mapping[str, tuple[tuple[str, str], ...]]:
        try:
            validate_topology_references(topology)
            topology_nodes = set(topology["nodes"])
            if topology_nodes != {node_id.value for node_id in fleet.nodes}:
                raise TypeError("topology nodes must exactly match fleet nodes")
            expected_hosts: dict[str, dict[str, Any]] = {
                node_id.value: {"hostname": record.hostname, "fabric": {}}
                for node_id, record in fleet.nodes.items()
                if record.lifecycle != "retired"
            }
            functions: dict[str, list[tuple[str, str]]] = {
                node: [] for node in self._nodes
            }
            counter_names = tuple(self.raw_schema["$defs"]["rdmaCounters"]["required"])
            baseline = self.rdma_baseline["rdma_counters_after"]
            if not isinstance(baseline, Mapping):
                raise TypeError("generic RDMA baseline counters are required")
            for link in topology["links"]:
                if not link.get("accepted") or link.get("kind") not in {"direct-rdma", "switched-rdma"}:
                    continue
                for endpoint in link["endpoints"]:
                    node = endpoint["node_id"]
                    if node not in functions:
                        continue
                    interface, hca = endpoint.get("interface"), endpoint.get("hca")
                    gid = endpoint.get("gid_index")
                    mtu, rate = endpoint.get("mtu"), endpoint.get("link_rate_mbps")
                    if (
                        not isinstance(interface, str) or _DEVICE_NAME.fullmatch(interface) is None
                        or not isinstance(hca, str) or _DEVICE_NAME.fullmatch(hca) is None
                        or not isinstance(gid, int) or isinstance(gid, bool)
                        or not isinstance(mtu, int) or isinstance(mtu, bool)
                        or not isinstance(rate, int) or isinstance(rate, bool)
                    ):
                        raise TypeError("accepted topology RDMA endpoint is incomplete")
                    functions[node].append((interface, hca))
                    expected_hosts[node]["fabric"][(interface, hca)] = {
                        "gid_index": gid, "mtu": mtu, "link_rate_mbps": rate,
                    }
                    for counter in counter_names:
                        value = baseline.get(f"{node}/{hca}/{counter}")
                        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                            raise TypeError(f"missing RDMA baseline for {node}/{hca}/{counter}")
            self.inventory = {"hosts": expected_hosts}
            return MappingProxyType({node: tuple(sorted(set(records))) for node, records in functions.items()})
        except (AttributeError, KeyError, TypeError, ValidationError) as error:
            raise LocalHealthError(f"invalid generic health topology or baseline: {error}") from error

    def collect(self) -> ClusterHealth:
        collected: dict[str, NodeHealth] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(self._nodes)), thread_name_prefix="node-health") as pool:
            futures = {pool.submit(self._probe, node): node for node in reversed(self._nodes)}
            for future in as_completed(futures):
                node = futures[future]
                try:
                    collected[node] = future.result()
                except LocalHealthError:
                    raise
                except (OSError, TypeError, ValueError):
                    collected[node] = self._empty_node("critical", "collector_internal_error")
        nodes = MappingProxyType({node: collected[node] for node in self._nodes})
        states = {node.status for node in nodes.values()}
        status = "critical" if states & {"critical", "unreachable"} else "warning" if "warning" in states else "healthy"
        result = ClusterHealth(
            1,
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            status,
            nodes,
        )
        try:
            self._result_validator.validate(result.to_dict())
        except Exception as error:
            raise LocalHealthError(f"generated node health violates schema: {error}") from error
        return result

    def _probe(self, node: str) -> NodeHealth:
        argv: list[str] = ["--json", "--cpu-sample-ms", str(self.cpu_sample_milliseconds)]
        for interface, hca in self._functions[node]:
            argv.extend(("--interface", interface, "--hca", hca))
        result = self.backend.run_script(
            node, self.collector, tuple(argv), self.timeout_seconds
        )
        failure = self._command_failure(result)
        if failure is not None:
            return failure
        try:
            raw = json.loads(result.stdout.decode("utf-8"))
            self._raw_validator.validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError):
            return self._empty_node("critical", "collector_malformed")
        return self._evaluate(node, raw)

    def _command_failure(self, result: CommandResult) -> NodeHealth | None:
        if result.timed_out:
            return self._empty_node("critical", "collector_timeout")
        if result.stdout_truncated or result.stderr_truncated:
            return self._empty_node("critical", "collector_truncated")
        if result.returncode == 255:
            return self._empty_node("unreachable", "ssh_unreachable")
        if result.returncode != 0:
            return self._empty_node("critical", "collector_nonzero")
        return None

    @staticmethod
    def _empty_node(status: str, error: str) -> NodeHealth:
        return NodeHealth.from_dict({
            "status": status, "errors": [error], "warnings": [],
            "identity": None, "cpu": None, "memory": None, "swap": None,
            "root_filesystem": None, "accelerator": None, "thermal_zones": [],
            "fabric": None, "services": None,
        })

    def _evaluate(self, node: str, raw: dict[str, Any]) -> NodeHealth:
        errors: set[str] = set()
        warnings: set[str] = set()
        expected = self.inventory["hosts"][node]
        identity = raw["identity"]
        if identity["hostname"] != expected["hostname"]:
            errors.add("hostname_mismatch")

        root = raw["root_filesystem"]
        if root["read_only"] is True:
            errors.add("root_filesystem_read_only")
        elif root["read_only"] is None:
            errors.add("root_filesystem_unavailable")
        available = root["available_bytes"]
        if isinstance(available, int) and available < _ROOT_FREE_WARNING_BYTES:
            warnings.add("root_free_low")
        elif available is None:
            warnings.add("root_capacity_unavailable")

        accelerator = raw["accelerator"]
        if not accelerator["available"]:
            errors.add("nvidia_unavailable")

        services = raw["services"]
        if services["earlyoom_enabled"] is True:
            errors.add("earlyoom_enabled")
        if services["earlyoom_active"] is True:
            errors.add("earlyoom_active")
        if services["earlyoom_enabled"] is None or services["earlyoom_active"] is None:
            warnings.add("earlyoom_state_unavailable")
        if not services["docker_available"]:
            warnings.add("docker_unavailable")

        swap_used = raw["swap"]["used_bytes"]
        if isinstance(swap_used, int) and swap_used > _SWAP_WARNING_BYTES:
            warnings.add("swap_used_high")
        elif swap_used is None:
            warnings.add("swap_telemetry_unavailable")

        thermal = raw["thermal_zones"]
        if not thermal:
            warnings.add("thermal_unavailable")
        for zone in thermal:
            for trip in zone["trip_points"]:
                if not trip["reached"]:
                    continue
                trip_type = trip["type"].lower()
                if trip_type == "critical":
                    errors.add("thermal_critical_trip")
                elif trip_type in {"hot", "passive"}:
                    warnings.add("thermal_trip")

        actual_functions = raw["fabric"]["functions"]
        expected_functions = self._functions[node]
        if len(actual_functions) != len(expected_functions):
            errors.add("fabric_mismatch")
        for index, expected_function in enumerate(expected_functions):
            if index >= len(actual_functions):
                break
            function = actual_functions[index]
            interface, hca = expected_function
            fabric = expected["fabric"]
            expected_link = fabric.get((interface, hca), fabric)
            if (
                function["interface"] != interface
                or function["hca"] != hca
                or function["operstate"] != "up"
                or function["carrier"] != 1
                or function["speed_mbps"] != expected_link["link_rate_mbps"]
                or function["mtu"] != expected_link["mtu"]
                or function["rdma_interface"] != interface
            ):
                errors.add("fabric_mismatch")
            if str(function["rdma_state"]).upper() != "ACTIVE":
                errors.add("rdma_inactive")
            for counter, current in function["counters"].items():
                baseline_key = f"{node}/{hca}/{counter}"
                accepted = self.rdma_baseline["rdma_counters_after"].get(baseline_key)
                if current is None or not isinstance(accepted, int):
                    errors.add("rdma_counter_unavailable")
                elif current > accepted:
                    errors.add("rdma_counter_above_baseline")

        if any(raw["cpu"].get(key) is None for key in ("utilization_percent", "load_1", "load_5", "load_15")):
            warnings.add("cpu_telemetry_unavailable")
        if any(raw["memory"].get(key) is None for key in ("total_bytes", "available_bytes", "used_bytes", "used_percent")):
            warnings.add("memory_telemetry_unavailable")

        normalized = self._rounded(raw)
        status = "critical" if errors else "warning" if warnings else "healthy"
        return NodeHealth.from_dict({
            "status": status,
            "errors": sorted(errors),
            "warnings": sorted(warnings),
            **{key: normalized[key] for key in (
                "identity", "cpu", "memory", "swap", "root_filesystem",
                "accelerator", "thermal_zones", "fabric", "services",
            )},
        })

    @classmethod
    def _rounded(cls, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {item_key: cls._rounded(item, item_key) for item_key, item in value.items()}
        if isinstance(value, list):
            return [cls._rounded(item) for item in value]
        if isinstance(value, float) and key.endswith("_percent"):
            return round(value, 1)
        return value
