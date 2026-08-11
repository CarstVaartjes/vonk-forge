from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path

import pytest

from cluster_profiles.catalog import Catalog, fingerprint
from cluster_profiles.fleet.loaders import load_fleet
from cluster_profiles.health import ClusterHealth, NodeHealth
from cluster_profiles.legacy_cli import CliDependencies, build_dependencies, main
from cluster_profiles.state import (
    ControllerState,
    LockBusy,
    LockNotStale,
    StateFormatError,
)
from cluster_profiles.switcher import PrepareNodeResult, PrepareReport, SwitchReport

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOT_IDS = {"node1": "1" * 32, "node2": "2" * 32}


class FakeStore:
    def __init__(
        self,
        state: ControllerState | None = None,
        *,
        stale_result: bool = False,
        stale_error: Exception | None = None,
    ) -> None:
        self.state = state or ControllerState.stopped()
        self.stale_result = stale_result
        self.stale_error = stale_error
        self.locked = False

    @contextmanager
    def acquire(self):
        self.locked = True
        try:
            yield self.load()
        finally:
            self.locked = False

    def load(self) -> ControllerState:
        return self.state

    def break_stale_lock(self) -> bool:
        if self.stale_error is not None:
            raise self.stale_error
        return self.stale_result


class FakeSwitcher:
    def __init__(
        self,
        *,
        workload_healthy: bool = True,
        prepare_report: PrepareReport | None = None,
    ) -> None:
        self.workload_healthy = workload_healthy
        self.health_calls: list[str] = []
        self.prepare_calls: list[str] = []
        self.prepare_report = prepare_report

    def switch_profile(
        self,
        target_id: str,
        *,
        restore_to: str | None = None,
        dry_run: bool = False,
    ) -> SwitchReport:
        return SwitchReport(
            target_profile=target_id,
            status="planned" if dry_run else "active",
            profile_sha256="a" * 64,
            definition_sha256={"fixture": "b" * 64},
            published_endpoints={},
            restore_profile=restore_to,
            dry_run=dry_run,
        )

    def workload_is_healthy(self, definition_id: str) -> bool:
        self.health_calls.append(definition_id)
        return self.workload_healthy

    def prepare_profile(self, target_id: str) -> PrepareReport:
        self.prepare_calls.append(target_id)
        if self.prepare_report is not None:
            return self.prepare_report
        return PrepareReport(
            target_profile=target_id,
            status="prepared",
            profile_sha256="a" * 64,
            definition_sha256={"deepseek-agent-dual": "b" * 64},
            results=(
                PrepareNodeResult(
                    workload="deepseek-agent-dual",
                    node="node2",
                    role="worker",
                    status="prepared",
                    timeout_seconds=86400,
                    returncode=0,
                    timed_out=False,
                ),
            ),
        )


@dataclass(frozen=True)
class Result:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict[str, object]:
        return json.loads(self.stdout)


def invoke(
    *argv: str,
    state: ControllerState | None = None,
    store: FakeStore | None = None,
    switcher: FakeSwitcher | None = None,
    catalog_value: Catalog | None = None,
    inventory_provider: Callable[[], Mapping[str, object]] | None = None,
    health_service=None,
) -> Result:
    catalog = catalog_value or Catalog.load(REPOSITORY_ROOT)
    dependencies = CliDependencies(
        catalog=catalog,
        state_store=store or FakeStore(state),
        switcher=switcher or FakeSwitcher(),
        inventory_provider=inventory_provider or live_inventory,
        health_service=health_service,
    )
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv, dependencies=dependencies)
    return Result(exit_code, stdout.getvalue(), stderr.getvalue())


class FakeHealthService:
    def __init__(self, result: ClusterHealth | Exception):
        self.result = result
        self.calls = 0

    def collect(self) -> ClusterHealth:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def health_result(*, node2_status="healthy") -> ClusterHealth:
    def node(name: str, status: str) -> NodeHealth:
        full = status not in {"unreachable", "critical"}
        return NodeHealth.from_dict({
            "status": status,
            "errors": ["ssh_unreachable"] if status == "unreachable" else [],
            "warnings": [],
            "identity": {"hostname": "node", "boot_id": BOOT_IDS[name], "uptime_seconds": 12345} if full else None,
            "cpu": {"logical_processors": 20, "utilization_percent": 12.3, "load_1": 1.2, "load_5": 1.0, "load_15": 0.8} if full else None,
            "memory": {"total_bytes": 130663231488, "available_bytes": 120000000000, "used_bytes": 10663231488, "used_percent": 8.2} if full else None,
            "swap": {"total_bytes": 0, "free_bytes": 0, "used_bytes": 0, "used_percent": 0.0} if full else None,
            "root_filesystem": {"total_bytes": 4031871553536, "available_bytes": 3787009835008, "used_bytes": 244861718528, "used_percent": 6.1, "read_only": False} if full else None,
            "accelerator": {"available": True, "name": "NVIDIA GB10", "driver_version": "580", "utilization_percent": 0.0, "temperature_c": 40.0, "performance_state": "P8", "power_watts": None} if full else None,
            "thermal_zones": [],
            "fabric": {"functions": [
                {"interface": interface, "hca": hca, "operstate": "up", "carrier": 1, "speed_mbps": 200000, "mtu": 1500, "rdma_interface": interface, "rdma_state": "ACTIVE", "counters": {}}
                for interface, hca in (("enp1s0f1np1", "rocep1s0f1"), ("enP2p1s0f1np1", "roceP2p1s0f1"))
            ]} if full else None,
            "services": {"docker_available": True, "docker_version": "29", "earlyoom_load_state": "not-found", "earlyoom_enabled": False, "earlyoom_active": False} if full else None,
        })
    nodes = {"node1": node("node1", "healthy"), "node2": node("node2", node2_status)}
    return ClusterHealth(1, "2026-08-02T12:00:00Z", "critical" if node2_status in {"critical", "unreachable"} else "healthy", nodes)


def live_inventory(
    *, boot_ids: Mapping[str, str] = BOOT_IDS,
    node2_healthy: bool = True,
) -> Mapping[str, object]:
    return {
        "node1": {
            "healthy": True,
            "free_memory_bytes": 120_000_000_000,
            "free_disk_bytes": 3_700_000_000_000,
            "boot_id": boot_ids["node1"],
        },
        "node2": {
            "healthy": node2_healthy,
            "free_memory_bytes": 120_000_000_000,
            "free_disk_bytes": 3_700_000_000_000,
            "boot_id": boot_ids["node2"],
        },
    }


def accepted_catalog(
    *,
    matching_maturity_fingerprint: bool = True,
    manifest_digest: bool = True,
    profile_evidence: bool = True,
) -> Catalog:
    base = Catalog.load(REPOSITORY_ROOT)
    identifier = "deepseek-agent-dual"
    original = base.definitions[identifier]
    definition = replace(
        original,
        checkpoint=replace(
            original.checkpoint,
            manifest_sha256="9" * 64 if manifest_digest else None,
        ),
    )
    definition_hash = fingerprint(definition)
    profile_hash = base.profile_fingerprints["agent-full-dual"]
    return Catalog(
        definitions={identifier: definition},
        profiles=base.profiles,
        selectors=base.selectors,
        definition_fingerprints={identifier: definition_hash},
        profile_fingerprints=base.profile_fingerprints,
        maturity={identifier: "accepted"},
        maturity_fingerprints={
            identifier: definition_hash
            if matching_maturity_fingerprint
            else "f" * 64
        },
        accepted_profiles={
            profile_hash: (definition_hash,)
        }
        if profile_evidence
        else {},
        package_families={},
        workload_deployments={},
        legacy_workload_deployments={},
    )


def legacy_exclusive_colocation_catalog() -> Catalog:
    base = accepted_catalog()
    original = base.definitions["deepseek-agent-dual"]
    definitions = tuple(
        replace(
            original,
            id=identifier,
            topology="single",
            placement_class="single-exclusive",
            nodes=("node1",),
            start_order=("node1",),
            stop_order=("node1",),
            paths=replace(
                original.paths,
                cache=Path(f"/srv/models/snapshots/{identifier}"),
                scratch=Path(f"/srv/models/runtime-cache/{identifier}"),
                output=Path(f"/srv/models/outputs/{identifier}"),
            ),
            endpoint=replace(original.endpoint, port=port),
        )
        for identifier, port in (("exclusive-one", 9001), ("exclusive-two", 9002))
    )
    profile = replace(
        base.profiles["agent-full-dual"],
        placements={
            "node1": tuple(definition.id for definition in definitions),
            "node2": (),
        },
        endpoints={"deepseek": "exclusive-one"},
    )
    definition_fingerprints = {
        definition.id: fingerprint(definition) for definition in definitions
    }
    profile_fingerprint = fingerprint(profile)
    return Catalog(
        definitions={definition.id: definition for definition in definitions},
        profiles={profile.id: profile},
        selectors=base.selectors,
        definition_fingerprints=definition_fingerprints,
        profile_fingerprints={profile.id: profile_fingerprint},
        maturity={definition.id: "accepted" for definition in definitions},
        maturity_fingerprints=definition_fingerprints,
        accepted_profiles={
            profile_fingerprint: tuple(sorted(definition_fingerprints.values()))
        },
        package_families={},
        workload_deployments={},
        legacy_workload_deployments={},
    )


def accepted_empty_catalog(*, endpoint_target: str | None = None) -> Catalog:
    base = accepted_catalog()
    profile = replace(
        base.profiles["agent-full-dual"],
        placements={"node1": (), "node2": ()},
        endpoints={"deepseek": endpoint_target} if endpoint_target else {},
    )
    profile_fingerprint = fingerprint(profile)
    return Catalog(
        definitions=base.definitions,
        profiles={profile.id: profile},
        selectors=base.selectors,
        definition_fingerprints=base.definition_fingerprints,
        profile_fingerprints={profile.id: profile_fingerprint},
        maturity=base.maturity,
        maturity_fingerprints=base.maturity_fingerprints,
        accepted_profiles={profile_fingerprint: ()},
        package_families={},
        workload_deployments={},
        legacy_workload_deployments={},
    )


def active_state(catalog: Catalog) -> ControllerState:
    profile = catalog.profiles["agent-full-dual"]
    identifiers = {
        identifier
        for node_identifiers in profile.placements.values()
        for identifier in node_identifiers
    }
    return ControllerState(
        status="active",
        active_profile=profile.id,
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=catalog.profile_fingerprints[profile.id],
        active_definition_sha256={
            identifier: catalog.definition_fingerprints[identifier]
            for identifier in identifiers
        },
        boot_ids=BOOT_IDS,
    )


def test_agent_alias_resolves_to_full_default() -> None:
    result = invoke("switch", "agent", "--dry-run", "--json")

    assert result.exit_code == 0
    assert result.json["target_profile"] == "agent-full-dual"
    assert result.json["status"] == "planned"


def test_prepare_resolves_selector_and_emits_a_dedicated_payload() -> None:
    switcher = FakeSwitcher()

    result = invoke("prepare", "default", "--json", switcher=switcher)

    assert result.exit_code == 0
    assert switcher.prepare_calls == ["agent-full-dual"]
    assert result.json == {
        "target_profile": "agent-full-dual",
        "status": "prepared",
        "profile_sha256": "a" * 64,
        "definition_sha256": {"deepseek-agent-dual": "b" * 64},
        "resumable": False,
        "results": [
            {
                "workload": "deepseek-agent-dual",
                "node": "node2",
                "role": "worker",
                "status": "prepared",
                "timeout_seconds": 86400,
                "returncode": 0,
                "timed_out": False,
                "detail": "",
            }
        ],
        "errors": [],
    }


def test_prepare_timeout_is_exit_eight_and_resumable() -> None:
    report = PrepareReport(
        target_profile="agent-full-dual",
        status="in-progress",
        profile_sha256="a" * 64,
        definition_sha256={"deepseek-agent-dual": "b" * 64},
        results=(
            PrepareNodeResult(
                workload="deepseek-agent-dual",
                node="node2",
                role="worker",
                status="in-progress",
                timeout_seconds=86400,
                returncode=None,
                timed_out=True,
                detail="timeout: no diagnostic output",
            ),
        ),
        errors=("prepare remains in progress",),
        resumable=True,
    )

    result = invoke(
        "prepare",
        "default",
        "--json",
        switcher=FakeSwitcher(prepare_report=report),
    )

    assert result.exit_code == 8
    assert result.json["status"] == "in-progress"
    assert result.json["resumable"] is True
    assert result.json["results"][0]["timed_out"] is True


def test_prepare_failed_and_blocked_have_distinct_exit_codes() -> None:
    reports = (
        (
            PrepareReport(
                target_profile="agent-full-dual",
                status="failed",
                profile_sha256="a" * 64,
                definition_sha256={},
                errors=("remote prepare failed",),
            ),
            6,
        ),
        (
            PrepareReport(
                target_profile="agent-full-dual",
                status="blocked",
                profile_sha256="a" * 64,
                definition_sha256={},
                errors=("controller is active",),
            ),
            3,
        ),
    )

    for report, expected_exit in reports:
        result = invoke(
            "prepare",
            "default",
            "--json",
            switcher=FakeSwitcher(prepare_report=report),
        )

        assert result.exit_code == expected_exit
        assert result.json["errors"] == list(report.errors)


def test_prepare_unknown_selector_and_lock_conflict_fail_closed() -> None:
    switcher = FakeSwitcher()
    unknown = invoke("prepare", "missing", "--json", switcher=switcher)

    assert unknown.exit_code == 2
    assert unknown.json["error_type"] == "configuration"
    assert switcher.prepare_calls == []

    class LockedPrepareSwitcher(FakeSwitcher):
        def prepare_profile(self, target_id: str) -> PrepareReport:
            raise LockBusy("prepare lock is held")

    locked = invoke(
        "prepare", "default", "--json", switcher=LockedPrepareSwitcher()
    )

    assert locked.exit_code == 7
    assert locked.json == {
        "error": "prepare lock is held",
        "error_type": "lock_conflict",
    }


def test_accepted_home_is_visible_and_admitted() -> None:
    result = invoke("validate", "default", "--json")

    assert result.json["profile_id"] == "agent-full-dual"
    assert result.json["valid"] is True
    assert result.json["admitted"] is True
    assert result.json["errors"] == []
    assert result.exit_code == 0


def test_endpoint_refuses_workload_when_controller_is_stopped() -> None:
    result = invoke("endpoint", "deepseek", "--json")

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "controller status is stopped",
    }


def test_status_is_a_local_stopped_snapshot() -> None:
    calls = 0

    def forbidden_live_inventory():
        nonlocal calls
        calls += 1
        raise AssertionError("status must stay local")

    result = invoke(
        "status", "--json", inventory_provider=forbidden_live_inventory
    )

    assert result.exit_code == 0
    assert result.json["status"] == "stopped"
    assert result.json["active_profile"] is None
    assert result.json["published_endpoints"] == {}
    assert calls == 0


def test_catalog_supports_global_json_and_shows_accepted_definition() -> None:
    result = invoke("--json", "catalog")
    per_command = invoke("catalog", "--json")

    assert result.exit_code == 0
    assert per_command.exit_code == 0
    assert per_command.json == result.json
    assert result.json["selectors"]["agent"] == "agent-full-dual"
    assert result.json["profiles"][0]["profile_id"] == "agent-full-dual"
    assert result.json["profiles"][0]["workloads"] == ["deepseek-agent-dual"]
    assert result.json["definitions"][0]["maturity"] == "accepted"


def test_restore_default_is_an_explicit_ordinary_switch() -> None:
    result = invoke("restore-default", "--dry-run", "--json")

    assert result.exit_code == 0
    assert result.json["target_profile"] == "agent-full-dual"
    assert result.json["restore_profile"] is None
    assert result.json["dry_run"] is True


def test_switch_only_records_canonical_restore_intent() -> None:
    result = invoke(
        "switch", "agent", "--restore", "default", "--dry-run", "--json"
    )

    assert result.exit_code == 0
    assert result.json["target_profile"] == "agent-full-dual"
    assert result.json["restore_profile"] == "agent-full-dual"


def test_break_stale_lock_reports_whether_a_lock_was_removed() -> None:
    result = invoke(
        "break-stale-lock", "--json", store=FakeStore(stale_result=True)
    )

    assert result.exit_code == 0
    assert result.json == {"broken": True}


def test_break_stale_lock_refuses_an_unsafe_override() -> None:
    result = invoke(
        "break-stale-lock",
        "--json",
        store=FakeStore(stale_error=LockNotStale("lock records live PID 123")),
    )

    assert result.exit_code == 7
    assert result.json == {
        "error": "lock records live PID 123",
        "error_type": "lock_conflict",
    }


def test_endpoint_refuses_matching_but_planned_active_content() -> None:
    catalog = Catalog.load(REPOSITORY_ROOT)
    profile = catalog.profiles["agent-full-dual"]
    state = ControllerState(
        status="active",
        active_profile=profile.id,
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=catalog.profile_fingerprints[profile.id],
        active_definition_sha256={
            "deepseek-agent-dual": catalog.definition_fingerprints[
                "deepseek-agent-dual"
            ]
        },
    )

    result = invoke("endpoint", "deepseek", "--json", state=state)

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "active state has no GPU node boot IDs",
    }


def test_status_hides_matching_but_planned_active_endpoints() -> None:
    catalog = Catalog.load(REPOSITORY_ROOT)
    profile = catalog.profiles["agent-full-dual"]
    state = ControllerState(
        status="active",
        active_profile=profile.id,
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=catalog.profile_fingerprints[profile.id],
        active_definition_sha256={
            "deepseek-agent-dual": catalog.definition_fingerprints[
                "deepseek-agent-dual"
            ]
        },
    )

    result = invoke("status", "--json", state=state)

    assert result.exit_code == 0
    assert result.json["published_endpoints"] == {}


@pytest.mark.parametrize(
    "catalog_value",
    (
        accepted_catalog(matching_maturity_fingerprint=False),
        accepted_catalog(manifest_digest=False),
        accepted_catalog(profile_evidence=False),
    ),
    ids=("stale-maturity-hash", "missing-manifest", "missing-profile-evidence"),
)
def test_status_requires_complete_current_acceptance_evidence(
    catalog_value: Catalog,
) -> None:
    result = invoke(
        "status",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
    )

    assert result.exit_code == 0
    assert result.json["published_endpoints"] == {}


def test_status_never_claims_live_endpoint_availability() -> None:
    catalog_value = accepted_catalog()

    result = invoke(
        "status",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=lambda: (_ for _ in ()).throw(
            AssertionError("status must not probe nodes")
        ),
    )

    assert result.exit_code == 0
    assert result.json["published_endpoints"] == {}


def test_endpoint_allows_exact_currently_accepted_content() -> None:
    catalog_value = accepted_catalog()
    inventory_calls = 0
    switcher = FakeSwitcher()

    def counting_inventory() -> Mapping[str, object]:
        nonlocal inventory_calls
        inventory_calls += 1
        return live_inventory()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=counting_inventory,
        switcher=switcher,
    )

    assert result.exit_code == 0
    assert result.json["available"] is True
    assert result.json["workload_id"] == "deepseek-agent-dual"
    assert inventory_calls == 1
    assert switcher.health_calls == ["deepseek-agent-dual"]


def test_endpoint_refuses_legacy_active_exclusive_colocation() -> None:
    catalog_value = legacy_exclusive_colocation_catalog()
    inventory_calls = 0
    switcher = FakeSwitcher()

    def counting_inventory() -> Mapping[str, object]:
        nonlocal inventory_calls
        inventory_calls += 1
        return live_inventory()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=counting_inventory,
        switcher=switcher,
    )

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "active profile violates current placement policy",
    }
    assert inventory_calls == 0
    assert switcher.health_calls == []


def test_empty_active_profile_has_no_endpoint() -> None:
    catalog_value = accepted_empty_catalog()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=lambda: (_ for _ in ()).throw(
            AssertionError("an empty profile must not probe nodes")
        ),
    )

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "endpoint is not published by active profile agent-full-dual",
    }


@pytest.mark.parametrize(
    "target", ("deepseek-agent-dual", "missing"), ids=("unassigned", "unknown")
)
def test_endpoint_refuses_legacy_empty_profile_endpoint(target: str) -> None:
    catalog_value = accepted_empty_catalog(endpoint_target=target)
    inventory_calls = 0
    switcher = FakeSwitcher()

    def counting_inventory() -> Mapping[str, object]:
        nonlocal inventory_calls
        inventory_calls += 1
        return live_inventory()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=counting_inventory,
        switcher=switcher,
    )

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "active profile violates current placement policy",
    }
    assert inventory_calls == 0
    assert switcher.health_calls == []


def test_endpoint_refuses_a_boot_id_mismatch() -> None:
    catalog_value = accepted_catalog()
    changed = {"node1": BOOT_IDS["node1"], "node2": "3" * 32}

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=lambda: live_inventory(boot_ids=changed),
    )

    assert result.exit_code == 3
    assert result.json["available"] is False
    assert result.json["reason"] == "GPU node boot IDs changed since activation"


def test_endpoint_refuses_an_unreachable_node() -> None:
    catalog_value = accepted_catalog()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=lambda: live_inventory(node2_healthy=False),
    )

    assert result.exit_code == 3
    assert result.json["available"] is False
    assert result.json["reason"] == "live GPU node health gate failed"


@pytest.mark.parametrize("malformed", (None, []), ids=("none", "list"))
def test_endpoint_refuses_malformed_live_inventory_without_traceback(
    malformed: object,
) -> None:
    catalog_value = accepted_catalog()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        inventory_provider=lambda: malformed,  # type: ignore[return-value]
    )

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "live GPU node health gate failed",
    }
    assert result.stderr == ""


def test_endpoint_refuses_a_dead_workload_on_healthy_nodes() -> None:
    catalog_value = accepted_catalog()
    switcher = FakeSwitcher(workload_healthy=False)

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
        switcher=switcher,
    )

    assert result.exit_code == 3
    assert result.json["available"] is False
    assert result.json["reason"] == "active workload health gate failed"
    assert switcher.health_calls == ["deepseek-agent-dual"]


def test_endpoint_refuses_state_changed_during_live_probe() -> None:
    catalog_value = accepted_catalog()
    store = FakeStore(active_state(catalog_value))

    def changing_inventory() -> Mapping[str, object]:
        assert store.locked is True
        store.state = ControllerState.stopped(boot_ids=BOOT_IDS)
        return live_inventory()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        store=store,
        catalog_value=catalog_value,
        inventory_provider=changing_inventory,
    )

    assert result.exit_code == 3
    assert result.json["available"] is False
    assert result.json["reason"] == "controller state changed during endpoint check"


def test_unknown_selector_is_a_configuration_error() -> None:
    result = invoke("validate", "does-not-exist", "--json")

    assert result.exit_code == 2
    assert result.json == {
        "error": "unknown cluster profile or selector: does-not-exist",
        "error_type": "configuration",
    }


def test_transition_failure_is_exit_six_and_redacts_bounded_errors() -> None:
    class FailedSwitcher(FakeSwitcher):
        def switch_profile(self, target_id, *, restore_to=None, dry_run=False):
            return SwitchReport(
                target_profile=target_id,
                status="stopped",
                profile_sha256="a" * 64,
                definition_sha256={},
                published_endpoints={},
                errors=("Authorization: Bearer supersecret " + "x" * 5_000,),
            )

    result = invoke("switch", "default", "--json", switcher=FailedSwitcher())

    assert result.exit_code == 6
    assert "supersecret" not in result.stdout
    assert "<redacted>" in result.stdout
    assert len(result.stdout) < 2_000


def test_human_status_is_readable_and_not_json() -> None:
    result = invoke("status")

    assert result.exit_code == 0
    assert result.stdout.startswith("status: stopped\n")
    assert not result.stdout.startswith("{")


def test_argument_errors_use_exit_two_and_json_when_requested() -> None:
    result = invoke("--json", "switch")

    assert result.exit_code == 2
    assert result.json["error_type"] == "arguments"
    assert "selector" in result.json["error"]


@pytest.mark.parametrize(
    ("option", "secret_value"),
    (
        ("--token", "token-value-123"),
        ("--api-key", "key-value-456"),
        ("--password", "password-value-789"),
        ("--authorization", "Bearer bearer-value-321"),
    ),
)
def test_argument_errors_never_echo_whitespace_separated_secrets(
    option: str, secret_value: str
) -> None:
    result = invoke("--json", "status", option, secret_value)

    assert result.exit_code == 2
    assert secret_value not in result.stdout
    assert result.json == {
        "error": "invalid command arguments",
        "error_type": "arguments",
    }


def test_default_dependencies_use_local_state_and_conservative_inventory(
    tmp_path: Path,
) -> None:
    dependencies = build_dependencies(
        REPOSITORY_ROOT, state_directory=tmp_path / "vonkctl"
    )

    assert dependencies.state_store.load().status == "stopped"
    fleet = load_fleet(REPOSITORY_ROOT / "inventory/fleet.toml")
    assert dependencies.inventory_provider() == {
        node_id.value: {} for node_id in fleet.nodes
    }
    assert dependencies.health_service is None
    assert not (tmp_path / "vonkctl").exists()


def test_generic_dry_run_reports_generated_node_ids_and_plan_digest() -> None:
    class GenericSwitcher(FakeSwitcher):
        def switch_profile(self, target_id, *, restore_to=None, dry_run=False):
            return SwitchReport(
                target_profile=target_id, status="planned", profile_sha256="a" * 64,
                definition_sha256={"model": "b" * 64}, published_endpoints={},
                dry_run=True,
                nodes=("spk_00000000000000000000000000000001",),
                placement_digests={"model": "c" * 64},
            )

    result = invoke("switch", "default", "--dry-run", "--json", switcher=GenericSwitcher())
    assert result.exit_code == 0
    assert result.json["nodes"] == ["spk_00000000000000000000000000000001"]
    assert result.json["placement_digests"] == {"model": "c" * 64}


def test_dependencies_prefer_generic_fleet_when_repository_contains_one(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    shutil.copytree(
        REPOSITORY_ROOT, repository,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".worktrees",
            "node_modules",
            "target",
            "__pycache__",
        ),
    )
    node_id = "spk_0000000000000000000000000000000a"
    (repository / "inventory/fleet.toml").write_text(f'''schema_version = 2

[nodes.{node_id}]
display_name = "portable"
hostname = "portable.local"
lifecycle = "ready"
[nodes.{node_id}.management]
host = "portable.local"
user = "operator"
port = 22
[nodes.{node_id}.labels]
pool = "default"
''')
    (repository / "inventory/topology.json").write_text(json.dumps({"schema_version": 1, "nodes": [node_id], "links": []}))

    dependencies = build_dependencies(repository, state_directory=tmp_path / "state")

    assert dependencies.inventory_provider() == {node_id: {}}
    assert set(dependencies.switcher.backend._aliases) == {node_id, "node1"}


def test_accepted_rdma_evidence_uses_current_node_identifiers() -> None:
    evidence = json.loads(
        (REPOSITORY_ROOT / "inventory/reports/rdma-nccl.json").read_text()
    )

    for counter_map_name in (
        "rdma_counters_before",
        "rdma_counters_after",
        "rdma_counter_deltas",
    ):
        counter_map = evidence[counter_map_name]
        assert not any(
            key.startswith(("spark1/", "spark2/")) for key in counter_map
        )
        assert {key.split("/", 1)[0] for key in counter_map} >= {"node1", "node2"}


def test_node_health_dependencies_use_the_health_specific_output_cap(tmp_path: Path) -> None:
    dependencies = build_dependencies(
        REPOSITORY_ROOT,
        state_directory=tmp_path / "vonkctl",
        include_health=True,
    )

    assert dependencies.health_service is not None
    assert dependencies.health_service.backend._output_limit == 262144


def test_bin_script_finds_the_repository_when_run_elsewhere(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            REPOSITORY_ROOT / "bin/vonkctl-legacy",
            "status",
            "--json",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "stopped"

    invalid = subprocess.run(
        [
            sys.executable,
            REPOSITORY_ROOT / "bin/vonkctl-legacy",
            "--json",
            "switch",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["error_type"] == "arguments"


def test_switch_rejects_unknown_selector_before_the_switcher() -> None:
    result = invoke("switch", "missing", "--json")

    assert result.exit_code == 2
    assert result.json == {
        "error": "unknown cluster profile or selector: missing",
        "error_type": "configuration",
    }


def test_active_profile_refuses_an_unpublished_endpoint() -> None:
    catalog_value = accepted_catalog()

    result = invoke(
        "endpoint",
        "unknown",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
    )

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "unknown",
        "reason": "endpoint is not published by active profile agent-full-dual",
    }


def test_endpoint_refuses_stale_active_fingerprints() -> None:
    catalog = Catalog.load(REPOSITORY_ROOT)
    state = ControllerState(
        status="active",
        active_profile="agent-full-dual",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256="f" * 64,
        active_definition_sha256={
            "deepseek-agent-dual": catalog.definition_fingerprints[
                "deepseek-agent-dual"
            ]
        },
    )

    result = invoke("endpoint", "deepseek", "--json", state=state)

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "active controller fingerprints do not match the catalog",
    }


def test_switch_lock_conflict_is_exit_seven() -> None:
    class LockedSwitcher(FakeSwitcher):
        def switch_profile(self, target_id, *, restore_to=None, dry_run=False):
            raise LockBusy("switch lock is held")

    result = invoke("switch", "default", "--json", switcher=LockedSwitcher())

    assert result.exit_code == 7
    assert result.json == {
        "error": "switch lock is held",
        "error_type": "lock_conflict",
    }


def test_malformed_local_state_is_a_bounded_configuration_error() -> None:
    class MalformedStore(FakeStore):
        def load(self):
            raise StateFormatError("state " + "x" * 5_000)

    result = invoke("status", "--json", store=MalformedStore())

    assert result.exit_code == 2
    assert result.json["error_type"] == "configuration"
    assert len(result.json["error"]) <= 1_024


@pytest.mark.parametrize(
    "argv", (("status", "--json"), ("endpoint", "deepseek", "--json"))
)
def test_state_load_oserror_is_a_bounded_configuration_error(
    argv: tuple[str, ...],
) -> None:
    class UnreadableStore(FakeStore):
        def load(self):
            raise OSError("local state read failed " + "x" * 5_000)

    result = invoke(*argv, store=UnreadableStore())

    assert result.exit_code == 2
    assert result.json["error_type"] == "configuration"
    assert len(result.json["error"]) <= 1_024


def test_stale_lock_oserror_is_a_bounded_configuration_error() -> None:
    result = invoke(
        "break-stale-lock",
        "--json",
        store=FakeStore(stale_error=OSError("local lock read failed")),
    )

    assert result.exit_code == 2
    assert result.json == {
        "error": "local lock read failed",
        "error_type": "configuration",
    }


def test_switch_oserror_is_configuration_exit_two_not_a_traceback() -> None:
    class UnreadableSwitcher(FakeSwitcher):
        def switch_profile(self, target_id, *, restore_to=None, dry_run=False):
            raise OSError("local state write failed")

    result = invoke("switch", "default", "--json", switcher=UnreadableSwitcher())

    assert result.exit_code == 2
    assert result.json == {
        "error": "local state write failed",
        "error_type": "configuration",
    }


def test_validate_inventory_oserror_is_bounded_and_sanitized() -> None:
    def unreadable_inventory() -> Mapping[str, object]:
        raise OSError("token=super-secret-value " + "x" * 5_000)

    result = invoke(
        "validate",
        "default",
        "--json",
        inventory_provider=unreadable_inventory,
    )

    assert result.exit_code == 2
    assert result.json["error_type"] == "configuration"
    assert "super-secret-value" not in result.stdout
    assert "<redacted>" in result.stdout
    assert len(result.json["error"]) <= 1_024
    assert result.stderr == ""


def test_validate_live_health_failure_is_bounded_and_sanitized() -> None:
    from cluster_profiles.health import LocalHealthError

    def failed_health_inventory() -> Mapping[str, object]:
        raise LocalHealthError("token=super-secret-value " + "x" * 5_000)

    result = invoke(
        "validate",
        "default",
        "--json",
        inventory_provider=failed_health_inventory,
    )

    assert result.exit_code == 5
    assert result.json["error_type"] == "health_configuration"
    assert "super-secret-value" not in result.stdout
    assert "<redacted>" in result.stdout
    assert len(result.json["error"]) <= 1_024
    assert result.stderr == ""


@pytest.mark.parametrize("malformed", (None, []), ids=("none", "list"))
def test_validate_rejects_malformed_live_inventory_without_traceback(
    malformed: object,
) -> None:
    result = invoke(
        "validate",
        "default",
        "--json",
        inventory_provider=lambda: malformed,  # type: ignore[return-value]
    )

    assert result.exit_code == 2
    assert result.json == {
        "error": "live inventory is malformed",
        "error_type": "configuration",
    }
    assert result.stderr == ""


def test_nodes_status_json_preserves_reachable_node_and_exits_four() -> None:
    service = FakeHealthService(health_result(node2_status="unreachable"))

    result = invoke("nodes", "status", "--json", health_service=service)

    assert result.exit_code == 4
    assert result.json["schema_version"] == 1
    assert list(result.json["nodes"]) == ["node1", "node2"]
    assert result.json["nodes"]["node1"]["status"] == "healthy"
    assert result.json["nodes"]["node2"]["status"] == "unreachable"
    assert service.calls == 1


def test_nodes_status_human_table_has_approved_columns() -> None:
    result = invoke("nodes", "status", health_service=FakeHealthService(health_result()))

    assert result.exit_code == 0
    header = result.stdout.splitlines()[0]
    for column in (
        "NODE", "STATE", "CPU", "LOAD1", "MEM AVAILABLE", "SWAP USED",
        "ROOT FREE", "GPU", "TEMP", "FABRIC", "UPTIME",
    ):
        assert column in header
    assert "node1" in result.stdout
    assert "2/2 up" in result.stdout


def test_nodes_status_warning_is_exit_zero() -> None:
    cluster = health_result()
    warning = replace(cluster.nodes["node2"], status="warning", warnings=("swap_used_high",))
    service = FakeHealthService(replace(cluster, status="warning", nodes={"node1": cluster.nodes["node1"], "node2": warning}))

    result = invoke("nodes", "status", "--json", health_service=service)

    assert result.exit_code == 0
    assert result.json["status"] == "warning"


def test_nodes_status_missing_local_health_assets_is_exit_five() -> None:
    from cluster_profiles.health import LocalHealthError

    result = invoke(
        "nodes", "status", "--json",
        health_service=FakeHealthService(LocalHealthError("collector missing token=secret")),
    )

    assert result.exit_code == 5
    assert result.json["error_type"] == "health_configuration"
    assert "secret" not in result.stdout
