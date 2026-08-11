from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import vonk_agent.probe as probe_module
from vonk_agent import nvidia_tools
from vonk_agent.deadlines import MonotonicDeadline
from vonk_agent.nvidia_tools import (
    NVIDIA_TOOL_NAMES,
    REVIEWED_BUNDLE_SHA256,
    REVIEWED_BUNDLE_VERSION,
    InstalledPolicy,
    InstalledToolSecurityError,
    open_verified_executable,
    open_verified_support_archive,
)
from vonk_agent.probe import (
    AGGREGATE_OUTPUT_LIMIT_BYTES,
    FIXED_PROCESS_ENVIRONMENT,
    TOTAL_PROBE_SECONDS,
    BoundedProcessRunner,
    PinnedNodeProbe,
    ProbeCollectorError,
    ProbeDeadlineExceeded,
    ProbeOutputLimitExceeded,
    ProbeResultLimitExceeded,
    ProcessOutcome,
    ProcessRequest,
)
from vonk_agent_protocol import canonical_message

TOOL_CONTRACT = {
    "device_identity": ("bin/device_identity.py", "1.1.0"),
    "hardware_config": ("bin/hardware_config.py", "1.0.0"),
    "firmware_reporter": ("bin/firmware_reporter.py", "1.0.0"),
    "os_build_identity": ("bin/os_build_identity.py", "1.0.0"),
    "driver_inventory_reporter": ("bin/driver_inventory_reporter.py", "1.0.0"),
    "spark_diagctl_health": ("bin/spark_diagctl.py", "1.1.0"),
    "reset_reason_reporter": ("bin/reset_reason_reporter.py", "1.1.0"),
}
COMMON_ARGUMENTS = ["--stdout-json", "--no-write-file", "--quiet"]
TOOL_DIGESTS = {
    "device_identity": "110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf",
    "hardware_config": "07c05c03f65e9b707bc18ebd2ec010ac1622701fa0b87858014a5b71fd1af5bb",
    "firmware_reporter": "c5887cb8b456295ea937a44cf05d8c1a3fa64b2ac8239f35be61e8deb358d387",
    "os_build_identity": "ee2f06d7ae25438ed0a7258eeeecdde76dba24c5c82f9dec510c361b9d75f6f9",
    "driver_inventory_reporter": "f5f90c05f077f1cd6fa387d1f6eac3b7f40b7d859c6e5886c73ec03629fdfc26",
    "spark_diagctl_health": "03de23664d3a24295ce605075be957328f47c24fa37afb7bbfe60988cbee42c2",
    "reset_reason_reporter": "212b49f894e4703cc85743217a0a9d9f2bb5891702266df84b907df960d83774",
}
SUPPORT_CONTRACT = {
    "bin/common/asset_id.py": ("35277c9d42c97960434f10e7f8dfda0a7e12cfbe00aec0d86ea88099c5ac9eca", 8072),
    "bin/common/cli_base.py": ("0b1f72a2056cbb5a3c717e7853b7f4d986a4b91b7920eadab68888b101f1b1da", 15147),
    "bin/common/output.py": ("6938255c277aa5b3b2e805a2cbfdc52d86c5d19910591cb42272a7eb280e2426", 9200),
    "bin/common/__init__.py": ("a3b4329f7500a2f9d95369ba32b3eb563c27a76d6d96d9f98dac1c1fc41b938a", 754),
}


def _executable(path: Path, body: bytes = b"#!/bin/sh\nexit 0\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(body)
    path.chmod(0o755)
    return hashlib.sha256(body).hexdigest()


def _process_is_gone(pid: int) -> bool:
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return True
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _assert_process_gone(pid: int, *, within_seconds: float = 2) -> None:
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if _process_is_gone(pid):
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pytest.fail(f"descendant {pid} survived probe cleanup")


def installed_policy(tmp_path: Path) -> tuple[InstalledPolicy, dict[str, object]]:
    root = tmp_path / "bundle"
    root.mkdir(mode=0o700)
    tools = []
    for name in NVIDIA_TOOL_NAMES:
        relative_path, version = TOOL_CONTRACT[name]
        executable = root / relative_path
        tools.append(
            {
                "name": name,
                "version": version,
                "executable": str(executable),
                "sha256": (_executable(executable), TOOL_DIGESTS[name])[1],
                "arguments": [*COMMON_ARGUMENTS, "health"] if name == "spark_diagctl_health" else COMMON_ARGUMENTS,
                "timeout_seconds": 2,
                "output_limit_bytes": 65536,
            }
        )
    collector = tmp_path / "libexec" / "collect-health"
    support_files = []
    for relative, (digest, size) in SUPPORT_CONTRACT.items():
        support = root / relative
        _executable(support, f"# fixture {relative}\n".encode())
        support.chmod(0o644)
        support_files.append({"relative_path": relative, "sha256": digest, "size_bytes": size})
    document: dict[str, object] = {
        "schema_version": 1,
        "bundle_version": REVIEWED_BUNDLE_VERSION,
        "bundle_sha256": REVIEWED_BUNDLE_SHA256,
        "bundle_root": str(root),
        "tools": tools,
        "support_files": support_files,
        "health": {
            "executable": str(collector),
            "sha256": _executable(collector),
            "cpu_sample_ms": 250,
            "fabric_pairs": [
                {"interface": "enp1s0f1np1", "hca": "rocep1s0f1"},
                {"interface": "enP2p1s0f1np1", "hca": "roceP2p1s0f1"},
            ],
            "timeout_seconds": 5,
            "output_limit_bytes": 131072,
        },
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o644)
    policy = InstalledPolicy._load_for_test(path)
    tools = tuple(
        replace(tool, sha256=hashlib.sha256(tool.executable.read_bytes()).hexdigest())
        for tool in policy.tools
    )
    support = tuple(
        replace(
            item,
            sha256=hashlib.sha256((policy.bundle_root / item.relative_path).read_bytes()).hexdigest(),
            size_bytes=(policy.bundle_root / item.relative_path).stat().st_size,
        )
        for item in policy.support_files
    )
    return replace(policy, tools=tools, support_files=support), document


def health_document() -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00Z",
            "identity": {
                "hostname": "secret-host",
                "boot_id": "11111111-2222-3333-4444-555555555555",
                "uptime_seconds": 123,
            },
            "cpu": {"logical_processors": 20, "utilization_percent": 12.5, "load_1": 1, "load_5": 2, "load_15": 3},
            "memory": {"total_bytes": 128000000000, "available_bytes": 120000000000, "used_bytes": 8000000000, "used_percent": 6.2},
            "swap": {"total_bytes": 0, "free_bytes": 0, "used_bytes": 0, "used_percent": 0},
            "root_filesystem": {"total_bytes": 1000, "available_bytes": 800, "used_bytes": 200, "used_percent": 20, "read_only": False},
            "accelerator": {"available": True, "name": "NVIDIA GB10", "driver_version": "580.173.02", "utilization_percent": 12, "temperature_c": 41, "performance_state": "P8", "power_watts": 4.25, "active_nvidia_compute_processes": 0},
            "thermal_zones": [{"zone": "thermal_zone0", "type": "cpu-thermal", "temperature_c": 41, "trip_points": [{"type": "critical", "temperature_c": 90, "reached": False}]}],
            "fabric": {"functions": [{"interface": "enp1s0f1np1", "hca": "rocep1s0f1", "operstate": "up", "carrier": 1, "speed_mbps": 200000, "mtu": 9000, "rdma_interface": "enp1s0f1np1", "rdma_state": "ACTIVE", "counters": {"packet_seq_err": 0, "unknown_counter": 99}}]},
            "services": {"docker_available": True, "docker_version": "29.2.1", "earlyoom_load_state": "not-found", "earlyoom_enabled": False, "earlyoom_active": False},
            "unknown": {"artifact_path": "/secret", "ip_address": "192.0.2.1"},
        },
        sort_keys=True,
    ).encode()


def tool_document(data=None, *, ok: bool = True) -> bytes:
    return json.dumps(
        {"ok": ok, "data": data or {}, "errors": ["raw secret"] if not ok else [], "meta": {}},
        sort_keys=True,
    ).encode()


class RecordingRunner:
    def __init__(self, outcomes: dict[str, ProcessOutcome] | None = None) -> None:
        self.requests: list[ProcessRequest] = []
        self.outcomes = outcomes or {}

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        self.requests.append(request)
        return self.outcomes.get(
            Path(request.argv[0]).name,
            ProcessOutcome(returncode=0, stdout=tool_document(), stderr=b""),
        )


def successful_runner(policy: InstalledPolicy) -> RecordingRunner:
    return RecordingRunner(
        {
            policy.health.executable.name: ProcessOutcome(0, health_document(), b""),
            "device_identity.py": ProcessOutcome(
                0,
                tool_document({"sys_vendor": "NVIDIA", "product_name": "Vonk Forge GPU node", "product_serial": "secret"}),
                b"",
            ),
        }
    )


def test_probe_invocation_is_entirely_fixed_by_installed_policy(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)

    evidence = PinnedNodeProbe(policy, _runner=runner).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )

    assert len(runner.requests) == 1 + len(NVIDIA_TOOL_NAMES)
    health = runner.requests[0]
    assert health.argv == (str(policy.health.executable), *policy.health.arguments)
    assert health.cwd == Path("/")
    assert health.env == FIXED_PROCESS_ENVIRONMENT
    assert health.shell is False
    assert health.stdin_closed is True
    assert health.close_fds is True
    assert health.new_process_group is True
    diagnostic = runner.requests[-2]
    assert diagnostic.argv == (
        str(policy.tools[-2].executable),
        "--stdout-json",
        "--no-write-file",
        "--quiet",
        "health",
    )
    assert diagnostic.cwd == policy.bundle_root
    assert evidence["nvidia"]["bundle_version"] == REVIEWED_BUNDLE_VERSION
    assert evidence["nvidia"]["bundle_sha256"] == REVIEWED_BUNDLE_SHA256
    assert evidence["nvidia"]["tools"]["device_identity"] == {
        "status": "ok",
        "version": "1.1.0",
        "sha256": policy.tools[0].sha256,
        "data": {"product_name": "Vonk Forge GPU node", "sys_vendor": "NVIDIA"},
    }
    rendered = canonical_message(evidence).lower()
    assert b"secret" not in rendered
    assert b"hostname" not in rendered
    assert b"root_filesystem" not in rendered
    assert b"artifact" not in rendered


def test_probe_renewal_after_start_reaches_health_and_tool_processes(
    tmp_path: Path,
) -> None:
    policy, _ = installed_policy(tmp_path)
    started = time.monotonic()

    class Clock:
        value = started

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    lease = MonotonicDeadline(
        datetime.now(UTC) + timedelta(milliseconds=100),
        started + 0.1,
    )
    base = successful_runner(policy)

    class RenewingRunner(RecordingRunner):
        def run(self, request: ProcessRequest) -> ProcessOutcome:
            outcome = super().run(request)
            if len(self.requests) == 1:
                clock.value = started + 0.2
                lease.extend(datetime.now(UTC) + timedelta(seconds=30))
            return outcome

    runner = RenewingRunner(base.outcomes)

    evidence = PinnedNodeProbe(
        policy,
        _runner=runner,
        _monotonic=clock,
    ).collect(lease)

    assert evidence["vonk_forge"]["identity"] == {"uptime_seconds": 123}
    assert clock.value > started + 0.1
    assert all(request.renewable_deadline is lease for request in runner.requests)
    tool_requests = [
        request for request in runner.requests if "PYTHONPATH" in request.env
    ]
    assert tool_requests
    assert all(request.renewable_deadline is lease for request in tool_requests)


def test_probe_renewal_never_extends_fixed_fifteen_second_total_cap(
    tmp_path: Path,
) -> None:
    policy, _ = installed_policy(tmp_path)
    started = time.monotonic()

    class Clock:
        value = started

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    lease = MonotonicDeadline(
        datetime.now(UTC) + timedelta(minutes=1),
        started + 60.0,
    )
    base = successful_runner(policy)

    class CapCrossingRunner(RecordingRunner):
        def run(self, request: ProcessRequest) -> ProcessOutcome:
            outcome = super().run(request)
            clock.value = started + 16.0
            return outcome

    runner = CapCrossingRunner(base.outcomes)

    with pytest.raises(ProbeDeadlineExceeded):
        PinnedNodeProbe(
            policy,
            _runner=runner,
            _monotonic=clock,
        ).collect(lease)

    assert len(runner.requests) == 1
    assert runner.requests[0].renewable_deadline is lease
    assert runner.requests[0].absolute_deadline is not None
    assert started < runner.requests[0].absolute_deadline <= (
        started + TOTAL_PROBE_SECONDS
    )


def test_existing_collector_is_normalized_to_compatible_fabric_runtime_evidence(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    evidence = PinnedNodeProbe(policy, _runner=successful_runner(policy)).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )["vonk_forge"]

    assert evidence["identity"] == {"uptime_seconds": 123}
    assert b"boot_id" not in canonical_message(evidence)
    assert evidence["storage"]["read_only"] is False
    assert evidence["fabric"]["functions"][0]["interface"] == "enp1s0f1np1"
    assert evidence["fabric"]["functions"][0]["counters"] == {"packet_seq_err": 0}
    assert evidence["runtime"]["docker_version"] == "29.2.1"


@pytest.mark.parametrize("count", (0, 3, 65535))
def test_collector_normalizes_only_bounded_compute_process_count(
    tmp_path, count: int
) -> None:
    policy, _ = installed_policy(tmp_path)
    document = json.loads(health_document())
    document["accelerator"]["active_nvidia_compute_processes"] = count
    runner = successful_runner(policy)
    runner.outcomes[policy.health.executable.name] = ProcessOutcome(
        0, json.dumps(document).encode(), b""
    )

    evidence = PinnedNodeProbe(policy, _runner=runner).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )["vonk_forge"]

    assert evidence["accelerator"]["active_nvidia_compute_processes"] == count


def test_existing_collector_drops_sensitive_shapes_even_from_allowlisted_fields(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    document = json.loads(health_document())
    document["captured_at"] = "192.0.2.1"
    document["accelerator"]["name"] = "/etc/passwd"
    document["accelerator"]["driver_version"] = (
        "11111111-2222-4333-8444-555555555555"
    )
    document["services"]["docker_version"] = "00:11:22:33:44:55"
    runner = successful_runner(policy)
    runner.outcomes[policy.health.executable.name] = ProcessOutcome(
        0, json.dumps(document).encode(), b""
    )

    evidence = PinnedNodeProbe(policy, _runner=runner).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )["vonk_forge"]

    rendered = canonical_message(evidence)
    assert b"192.0.2.1" not in rendered
    assert b"/etc/passwd" not in rendered
    assert b"11111111-2222-4333-8444-555555555555" not in rendered
    assert b"00:11:22:33:44:55" not in rendered


def test_missing_nvidia_tool_is_unavailable_and_collector_still_runs(tmp_path) -> None:
    policy, document = installed_policy(tmp_path)
    Path(document["tools"][0]["executable"]).unlink()
    runner = successful_runner(policy)

    evidence = PinnedNodeProbe(policy, _runner=runner).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )

    assert evidence["nvidia"]["tools"]["device_identity"] == {
        "status": "unavailable",
        "version": "1.1.0",
        "sha256": policy.tools[0].sha256,
    }
    assert str(policy.health.executable) == runner.requests[0].argv[0]
    assert all(request.argv[0] != "device_identity" for request in runner.requests)


def test_missing_bundle_marks_every_nvidia_tool_unavailable_and_runs_collector(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    policy.bundle_root.rename(tmp_path / "bundle-not-installed")
    runner = successful_runner(policy)

    evidence = PinnedNodeProbe(policy, _runner=runner).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )

    assert len(runner.requests) == 1
    assert runner.requests[0].argv[0] == str(policy.health.executable)
    assert all(
        item["status"] == "unavailable"
        for item in evidence["nvidia"]["tools"].values()
    )


def test_present_tampered_tool_is_hard_failure_before_any_execution(tmp_path) -> None:
    policy, document = installed_policy(tmp_path)
    Path(document["tools"][0]["executable"]).write_text("tampered", encoding="utf-8")
    runner = successful_runner(policy)

    with pytest.raises(InstalledToolSecurityError):
        PinnedNodeProbe(policy, _runner=runner).collect(
            datetime.now(UTC) + timedelta(seconds=30)
        )

    assert runner.requests == []


def test_unexpected_common_module_is_hard_failure_before_any_execution(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    (policy.bundle_root / "bin/common/sitecustomize.py").write_text(
        "raise RuntimeError('attacker code')\n", encoding="utf-8"
    )
    runner = successful_runner(policy)

    with pytest.raises(InstalledToolSecurityError):
        PinnedNodeProbe(policy, _runner=runner).collect(
            datetime.now(UTC) + timedelta(seconds=30)
        )

    assert runner.requests == []


def test_nonzero_and_incompatible_tool_output_are_redacted_capability_evidence(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    runner.outcomes["hardware_config.py"] = ProcessOutcome(
        23, b"raw stdout secret", b"raw stderr secret"
    )
    runner.outcomes["firmware_reporter.py"] = ProcessOutcome(
        0, b"not-json-secret", b""
    )

    evidence = PinnedNodeProbe(policy, _runner=runner).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )

    assert evidence["nvidia"]["tools"]["hardware_config"] == {
        "status": "degraded",
        "version": "1.0.0",
        "sha256": policy.tools[1].sha256,
        "error_code": "tool_nonzero_exit",
    }
    assert evidence["nvidia"]["tools"]["firmware_reporter"] == {
        "status": "unsupported",
        "version": "1.0.0",
        "sha256": policy.tools[2].sha256,
        "error_code": "tool_output_incompatible",
    }
    assert b"secret" not in canonical_message(evidence)


def test_reported_tool_failure_accepts_null_data_as_redacted_degraded_evidence(
    tmp_path,
) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    runner.outcomes["hardware_config.py"] = ProcessOutcome(
        0,
        b'{"ok":false,"data":null,"errors":["raw secret"],"meta":{}}',
        b"",
    )

    evidence = PinnedNodeProbe(policy, _runner=runner).collect(
        datetime.now(UTC) + timedelta(seconds=30)
    )

    assert evidence["nvidia"]["tools"]["hardware_config"] == {
        "status": "degraded",
        "version": "1.0.0",
        "sha256": policy.tools[1].sha256,
        "data": {},
        "error_code": "tool_reported_failure",
    }
    assert b"secret" not in canonical_message(evidence)


def test_probe_rejects_expired_deadline_without_verifying_or_running(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)

    with pytest.raises(ProbeDeadlineExceeded):
        PinnedNodeProbe(policy, _runner=runner).collect(
            datetime.now(UTC) - timedelta(milliseconds=1)
        )

    assert runner.requests == []


def test_early_claim_deadline_is_shared_by_every_process(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    deadline = datetime.now(UTC) + timedelta(milliseconds=500)

    PinnedNodeProbe(policy, _runner=runner).collect(deadline)

    leases = [request.renewable_deadline for request in runner.requests]
    assert leases and all(lease is leases[0] for lease in leases)
    assert leases[0] is not None and leases[0].wall_deadline == deadline
    assert 0 < leases[0].remaining() <= 0.5


def test_broken_runner_cannot_return_last_tool_success_after_total_deadline(
    tmp_path,
) -> None:
    policy, _ = installed_policy(tmp_path)

    class Clock:
        value = time.monotonic()

        def __call__(self) -> float:
            return self.value

    clock = Clock()

    class DeadlineCrossingRunner(RecordingRunner):
        def run(self, request: ProcessRequest) -> ProcessOutcome:
            outcome = super().run(request)
            if len(self.requests) == 1 + len(NVIDIA_TOOL_NAMES):
                clock.value += 16
            return outcome

    base = successful_runner(policy)
    runner = DeadlineCrossingRunner(base.outcomes)

    with pytest.raises(ProbeDeadlineExceeded):
        PinnedNodeProbe(policy, _runner=runner, _monotonic=clock).collect(
            datetime.now(UTC) + timedelta(seconds=30)
        )


@pytest.mark.parametrize("stage", ["last-tool-normalization", "canonical-result"])
def test_normalization_or_result_construction_cannot_finish_after_deadline(
    tmp_path, monkeypatch, stage
) -> None:
    policy, _ = installed_policy(tmp_path)

    class Clock:
        value = time.monotonic()

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    if stage == "last-tool-normalization":
        original = probe_module.parse_tool_document
        calls = 0

        def delayed_parse(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == len(NVIDIA_TOOL_NAMES):
                clock.value += 16
            return result

        monkeypatch.setattr(probe_module, "parse_tool_document", delayed_parse)
    else:
        original_canonical = probe_module.canonical_message

        def delayed_canonical(value):
            result = original_canonical(value)
            clock.value += 16
            return result

        monkeypatch.setattr(probe_module, "canonical_message", delayed_canonical)

    with pytest.raises(ProbeDeadlineExceeded):
        PinnedNodeProbe(
            policy,
            _runner=successful_runner(policy),
            _monotonic=clock,
        ).collect(datetime.now(UTC) + timedelta(seconds=30))


def test_total_deadline_includes_installed_file_verification(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    ticks = iter((0.0, 16.0))
    probe = PinnedNodeProbe(policy, _runner=runner, _monotonic=lambda: next(ticks))

    with pytest.raises(ProbeDeadlineExceeded):
        probe.collect(datetime.now(UTC) + timedelta(seconds=30))

    assert runner.requests == []


def test_verification_stops_at_deadline_between_bounded_read_chunks(
    tmp_path, monkeypatch
) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    fixed_now = datetime.now(UTC)

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    original_read = nvidia_tools.os.read
    reads = 0

    def slow_read(descriptor: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        result = original_read(descriptor, size)
        clock.value += 0.06
        return result

    monkeypatch.setattr(nvidia_tools.os, "read", slow_read)

    with pytest.raises(ProbeDeadlineExceeded):
        PinnedNodeProbe(
            policy,
            _runner=runner,
            _monotonic=clock,
            _utcnow=lambda: fixed_now,
        ).collect(fixed_now + timedelta(milliseconds=50))

    assert reads == 1
    assert runner.requests == []


def test_slow_verification_read_cannot_overrun_claim_by_hundreds_of_milliseconds(
    tmp_path, monkeypatch
) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    original_read = nvidia_tools.os.read
    reads = 0

    def slow_read(descriptor: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        time.sleep(0.06)
        return original_read(descriptor, size)

    monkeypatch.setattr(nvidia_tools.os, "read", slow_read)
    started = time.monotonic()
    with pytest.raises(ProbeDeadlineExceeded):
        PinnedNodeProbe(policy, _runner=runner).collect(
            datetime.now(UTC) + timedelta(milliseconds=50)
        )

    assert time.monotonic() - started < 0.15
    assert reads == 1
    assert runner.requests == []


def test_adapter_enforces_aggregate_raw_output_even_against_a_broken_runner(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    runner.outcomes[policy.health.executable.name] = ProcessOutcome(
        0,
        b"x" * AGGREGATE_OUTPUT_LIMIT_BYTES,
        b"y",
    )

    with pytest.raises(ProbeOutputLimitExceeded):
        PinnedNodeProbe(policy, _runner=runner).collect(
            datetime.now(UTC) + timedelta(seconds=30)
        )


def test_canonical_result_limit_is_checked_after_allowlist_normalization(tmp_path) -> None:
    policy, _ = installed_policy(tmp_path)
    runner = successful_runner(policy)
    records = [{"name": "n" * 240, "current_version": "v" * 240, "vendor": "NVIDIA"} for _ in range(64)]
    runner.outcomes["firmware_reporter.py"] = ProcessOutcome(
        0, tool_document({"fwupd": {"available": True, "fwupdmgr_version": "2.0", "devices": records}}), b""
    )
    modules = [{"module": "n" * 240, "modinfo": {"version": "v" * 240, "license": "MIT"}} for _ in range(64)]
    runner.outcomes["driver_inventory_reporter.py"] = ProcessOutcome(
        0, tool_document({"drivers_manifest": modules}), b""
    )

    with pytest.raises(ProbeResultLimitExceeded):
        PinnedNodeProbe(policy, _runner=runner).collect(
            datetime.now(UTC) + timedelta(seconds=30)
        )


def test_real_runner_kills_process_group_on_timeout(tmp_path) -> None:
    script = tmp_path / "timeout.sh"
    child_file = tmp_path / "child.pid"
    digest = _executable(
        script,
        b"#!/bin/sh\nsleep 60 &\necho $! > child.pid\nwait\n",
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),),
        cwd=tmp_path,
        timeout_seconds=0.2,
        output_limit_bytes=4096,
        executable_fd=descriptor,
    )

    try:
        with pytest.raises(ProbeDeadlineExceeded):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    child_pid = int(child_file.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("timed-out child process group survived")


def test_real_runner_observes_renewed_deadline_after_process_start(tmp_path) -> None:
    script = tmp_path / "renewed.sh"
    digest = _executable(script, b"#!/bin/sh\nsleep 0.25\n")
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    initial_wall = datetime.now(UTC) + timedelta(seconds=0.12)
    lease = MonotonicDeadline.bind(initial_wall)
    request = ProcessRequest.fixed(
        argv=(str(script),),
        cwd=tmp_path,
        timeout_seconds=1,
        output_limit_bytes=1024,
        executable_fd=descriptor,
        renewable_deadline=lease,
    )

    def renew() -> None:
        time.sleep(0.05)
        lease.extend(datetime.now(UTC) + timedelta(seconds=1))

    renewer = threading.Thread(target=renew)
    renewer.start()
    try:
        outcome = BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)
    renewer.join(2)

    assert outcome.returncode == 0
    assert datetime.now(UTC) > initial_wall


def test_real_runner_bounds_capture_and_kills_output_flood(tmp_path) -> None:
    script = tmp_path / "flood.sh"
    digest = _executable(script, b"#!/bin/sh\nwhile :; do printf 'xxxxxxxxxxxxxxxx'; done\n")
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),),
        cwd=tmp_path,
        timeout_seconds=2,
        output_limit_bytes=1024,
        executable_fd=descriptor,
    )

    try:
        with pytest.raises(ProbeOutputLimitExceeded):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)


def test_real_runner_enforces_per_tool_timeout_below_total_deadline(tmp_path) -> None:
    script = tmp_path / "slow.sh"
    digest = _executable(script, b"#!/bin/sh\nsleep 0.5\n")
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),),
        cwd=tmp_path,
        timeout_seconds=0.1,
        output_limit_bytes=1024,
        executable_fd=descriptor,
        absolute_deadline=time.monotonic() + 3,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProbeDeadlineExceeded):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert time.monotonic() - started < 0.5


def test_tool_exit_124_is_generic_nonzero_not_supervisor_timeout(tmp_path) -> None:
    script = tmp_path / "exit-124.sh"
    digest = _executable(script, b"#!/bin/sh\nexit 124\n")
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    try:
        outcome = BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert outcome.returncode == 125


def test_real_runner_detects_deadline_crossed_during_spawn(tmp_path, monkeypatch) -> None:
    script = tmp_path / "quick.sh"
    digest = _executable(script)
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
        absolute_deadline=time.monotonic() + 0.05,
    )
    original_popen = probe_module.subprocess.Popen

    def slow_popen(*args, **kwargs):
        time.sleep(0.08)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(probe_module.subprocess, "Popen", slow_popen)
    started = time.monotonic()
    try:
        with pytest.raises(ProbeDeadlineExceeded):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)
    assert time.monotonic() - started < 0.15


def test_cleanup_deadline_cannot_turn_expired_execution_into_success(tmp_path) -> None:
    script = tmp_path / "slow-cleanup.py"
    digest = _executable(
        script,
        b"#!/usr/bin/env python3\n"
        b"import os, signal, time\n"
        b"if os.fork() == 0:\n"
        b"    os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        b"    null = os.open('/dev/null', os.O_RDWR)\n"
        b"    os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
        b"    time.sleep(60); os._exit(0)\n"
        b"time.sleep(0.02)\n",
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
        absolute_deadline=time.monotonic() + 0.08,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProbeDeadlineExceeded):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert time.monotonic() - started < 0.18


def test_stopped_supervisor_is_resumed_for_bounded_descendant_cleanup(tmp_path) -> None:
    script = tmp_path / "stop-supervisor.py"
    pid_file = tmp_path / "stopped-supervisor-child.pid"
    digest = _executable(
        script,
        b"#!/usr/bin/env python3\n"
        b"import os, signal, time\n"
        b"open('stopped-supervisor-child.pid', 'w').write(str(os.getpid()))\n"
        b"os.kill(os.getppid(), signal.SIGSTOP)\n"
        b"time.sleep(60)\n",
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProbeDeadlineExceeded):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert time.monotonic() - started < 1.5
    _assert_process_gone(int(pid_file.read_text()))


def test_guardian_crash_is_not_misreported_as_a_tool_exit(tmp_path) -> None:
    script = tmp_path / "crash-supervisor.py"
    pid_file = tmp_path / "crash-supervisor-tool.pid"
    digest = _executable(
        script,
        b"#!/usr/bin/env python3\n"
        b"import os, signal, time\n"
        b"open('crash-supervisor-tool.pid', 'w').write(str(os.getpid()))\n"
        b"null = os.open('/dev/null', os.O_RDWR)\n"
        b"os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
        b"os.kill(os.getppid(), signal.SIGKILL)\n"
        b"time.sleep(60)\n",
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProbeCollectorError):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert time.monotonic() - started < 0.3
    _assert_process_gone(int(pid_file.read_text()), within_seconds=0.3)


@pytest.mark.parametrize("escape", ["same-group", "setsid-double-fork"])
def test_guardian_crash_after_descendant_escape_is_contained(
    tmp_path, escape
) -> None:
    script = tmp_path / f"crash-guardian-{escape}.py"
    tool_pid_file = tmp_path / f"crash-guardian-{escape}-tool.pid"
    child_pid_file = tmp_path / f"crash-guardian-{escape}-child.pid"
    child_setup = (
        "if os.fork() == 0:\n"
        if escape == "same-group"
        else (
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    if os.fork() != 0: os._exit(0)\n"
        )
    )
    digest = _executable(
        script,
        (
            "#!/usr/bin/env python3\n"
            "import os, signal, time\n"
            "from pathlib import Path\n"
            f"open('{tool_pid_file.name}', 'w').write(str(os.getpid()))\n"
            f"{child_setup}"
            f"    open('{child_pid_file.name}', 'w').write(str(os.getpid()))\n"
            "    null = os.open('/dev/null', os.O_RDWR)\n"
            "    os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
            "    time.sleep(60); os._exit(0)\n"
            f"while not Path('{child_pid_file.name}').exists(): time.sleep(0.001)\n"
            "os.kill(os.getppid(), signal.SIGKILL)\n"
            "time.sleep(60)\n"
        ).encode(),
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    try:
        with pytest.raises(ProbeCollectorError):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    _assert_process_gone(int(tool_pid_file.read_text()), within_seconds=0.3)
    _assert_process_gone(int(child_pid_file.read_text()), within_seconds=0.3)


@pytest.mark.parametrize("escape", ["same-group", "setsid-double-fork"])
def test_outer_supervisor_crash_is_cleaned_by_surviving_guardian(
    tmp_path, escape
) -> None:
    script = tmp_path / f"crash-outer-{escape}.py"
    tool_pid_file = tmp_path / f"crash-outer-{escape}-tool.pid"
    child_pid_file = tmp_path / f"crash-outer-{escape}-child.pid"
    child_setup = (
        "if os.fork() == 0:\n"
        if escape == "same-group"
        else (
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    if os.fork() != 0: os._exit(0)\n"
        )
    )
    digest = _executable(
        script,
        (
            "#!/usr/bin/env python3\n"
            "import os, signal, time\n"
            "from pathlib import Path\n"
            f"open('{tool_pid_file.name}', 'w').write(str(os.getpid()))\n"
            "guardian = os.getppid()\n"
            "raw = Path(f'/proc/{guardian}/stat').read_text()\n"
            "supervisor = int(raw[raw.rfind(')') + 2:].split()[1])\n"
            f"{child_setup}"
            f"    open('{child_pid_file.name}', 'w').write(str(os.getpid()))\n"
            "    null = os.open('/dev/null', os.O_RDWR)\n"
            "    os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
            "    time.sleep(60); os._exit(0)\n"
            f"while not Path('{child_pid_file.name}').exists(): time.sleep(0.001)\n"
            "os.kill(supervisor, signal.SIGKILL)\n"
            "time.sleep(60)\n"
        ).encode(),
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProbeCollectorError):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert time.monotonic() - started < 0.35
    _assert_process_gone(int(tool_pid_file.read_text()), within_seconds=0.3)
    _assert_process_gone(int(child_pid_file.read_text()), within_seconds=0.3)


def test_authenticated_fallback_signals_only_the_pidfd_bound_guardian(
    monkeypatch,
) -> None:
    pidfd_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        probe_module.signal,
        "pidfd_send_signal",
        lambda pidfd, signum: pidfd_signals.append((pidfd, signum)),
    )
    poller = type(
        "ReadyPidfd",
        (),
        {"register": lambda *_: None, "poll": lambda *_: [7]},
    )()
    monkeypatch.setattr(probe_module.select, "poll", lambda: poller)

    probe_module._terminate_guardian(7)

    assert pidfd_signals == [(7, signal.SIGTERM), (7, signal.SIGCONT)]


def test_supervised_tool_launch_failure_is_bounded_and_typed(tmp_path) -> None:
    executable = tmp_path / "invalid-executable"
    digest = _executable(executable, b"not an executable format")
    descriptor = open_verified_executable(
        executable, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(executable),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    try:
        with pytest.raises(ProbeCollectorError):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)


def test_real_runner_kills_successful_daemonized_child(tmp_path) -> None:
    script = tmp_path / "daemon.sh"
    child_file = tmp_path / "daemon-child.pid"
    digest = _executable(
        script,
        b"#!/bin/sh\nsleep 60 </dev/null >/dev/null 2>&1 &\necho $! > daemon-child.pid\nexit 0\n",
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    try:
        assert BoundedProcessRunner().run(request).returncode == 0
    finally:
        os.close(descriptor)

    child_pid = int(child_file.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("successful daemonized child survived probe cleanup")


def test_real_runner_kills_descendant_that_escapes_to_a_new_session(tmp_path) -> None:
    script = tmp_path / "setsid.py"
    child_file = tmp_path / "setsid-child.pid"
    digest = _executable(
        script,
        b"#!/usr/bin/env python3\n"
        b"import os, time\n"
        b"if os.fork() == 0:\n"
        b"    os.setsid()\n"
        b"    if os.fork() != 0:\n"
        b"        os._exit(0)\n"
        b"    open('setsid-child.pid', 'w').write(str(os.getpid()))\n"
        b"    null = os.open('/dev/null', os.O_RDWR)\n"
        b"    os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
        b"    time.sleep(60)\n"
        b"    os._exit(0)\n"
        b"time.sleep(0.1)\n",
    )
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    try:
        assert BoundedProcessRunner().run(request).returncode == 0
    finally:
        os.close(descriptor)

    _assert_process_gone(int(child_file.read_text()))


@pytest.mark.parametrize("failure", ["timeout", "overflow"])
def test_real_runner_kills_setsid_descendant_on_bounded_failure(
    tmp_path, failure
) -> None:
    script = tmp_path / f"setsid-{failure}.py"
    child_file = tmp_path / f"setsid-{failure}.pid"
    parent_action = (
        "time.sleep(60)"
        if failure == "timeout"
        else "\nwhile True:\n    os.write(1, b'x' * 4096)"
    )
    body = (
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        f"    open('{child_file.name}', 'w').write(str(os.getpid()))\n"
        "    null = os.open('/dev/null', os.O_RDWR)\n"
        "    os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "time.sleep(0.1)\n"
        f"{parent_action}\n"
    ).encode()
    digest = _executable(script, body)
    descriptor = open_verified_executable(
        script, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest.fixed(
        argv=(str(script),), cwd=tmp_path, timeout_seconds=0.25,
        output_limit_bytes=1024, executable_fd=descriptor,
    )
    expected = ProbeDeadlineExceeded if failure == "timeout" else ProbeOutputLimitExceeded
    try:
        with pytest.raises(expected):
            BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    _assert_process_gone(int(child_file.read_text()))


def test_concurrent_runners_contain_only_their_own_escaped_descendants(tmp_path) -> None:
    unrelated = subprocess.Popen(
        ["/usr/bin/sleep", "5"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    requests: list[tuple[ProcessRequest, int, Path]] = []
    for index in range(2):
        script = tmp_path / f"concurrent-{index}.py"
        pid_file = tmp_path / f"concurrent-{index}.pid"
        body = (
            "#!/usr/bin/env python3\n"
            "import os, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            f"    open('{pid_file.name}', 'w').write(str(os.getpid()))\n"
            "    null = os.open('/dev/null', os.O_RDWR)\n"
            "    os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
            "    time.sleep(60)\n"
            "    os._exit(0)\n"
            "time.sleep(0.2)\n"
        ).encode()
        digest = _executable(script, body)
        descriptor = open_verified_executable(
            script, digest, _test_only_allow_unprivileged=True
        )
        assert descriptor is not None
        requests.append(
            (
                ProcessRequest.fixed(
                    argv=(str(script),), cwd=tmp_path, timeout_seconds=1,
                    output_limit_bytes=1024, executable_fd=descriptor,
                ),
                descriptor,
                pid_file,
            )
        )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    BoundedProcessRunner().run,
                    [request for request, _, _ in requests],
                )
            )
        assert [item.returncode for item in outcomes] == [0, 0]
        assert unrelated.poll() is None
    finally:
        for _, descriptor, _ in requests:
            os.close(descriptor)
        unrelated.terminate()
        unrelated.wait(timeout=2)

    for _, _, pid_file in requests:
        _assert_process_gone(int(pid_file.read_text()))


def test_process_request_cannot_enable_shell_stdin_or_inherited_descriptors(tmp_path) -> None:
    executable = tmp_path / "tool"
    digest = _executable(executable)
    descriptor = open_verified_executable(
        executable, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    try:
        request = ProcessRequest.fixed(
            argv=(str(executable), "--json"),
            cwd=tmp_path,
            timeout_seconds=1,
            output_limit_bytes=1024,
            executable_fd=descriptor,
        )

        assert request.shell is False
        assert request.stdin_closed is True
        assert request.close_fds is True
        assert request.new_process_group is True
        assert request.inherited_fds == (descriptor,)
        with pytest.raises(AttributeError):
            request.argv = ("id",)  # type: ignore[misc]
    finally:
        os.close(descriptor)


def test_process_request_rejects_duplicate_or_reserved_additional_descriptors(tmp_path) -> None:
    executable = tmp_path / "tool"
    digest = _executable(executable)
    executable_fd = open_verified_executable(
        executable, digest, _test_only_allow_unprivileged=True
    )
    assert executable_fd is not None
    extra_fd = os.memfd_create("extra", os.MFD_CLOEXEC)
    try:
        for additional in (
            (executable_fd,),
            (extra_fd, extra_fd),
        ):
            with pytest.raises(ProbeCollectorError):
                ProcessRequest.fixed(
                    argv=(str(executable),),
                    cwd=tmp_path,
                    timeout_seconds=1,
                    output_limit_bytes=1024,
                    executable_fd=executable_fd,
                    additional_fds=additional,
                )
        with pytest.raises(ProbeCollectorError):
            ProcessRequest.fixed(
                argv=(str(executable),),
                cwd=tmp_path,
                timeout_seconds=1,
                output_limit_bytes=1024,
                executable_fd=executable_fd,
                support_archive_fd=extra_fd,
                additional_fds=(extra_fd,),
            )
    finally:
        os.close(extra_fd)
        os.close(executable_fd)


def test_verified_descriptor_execution_resists_path_swap(tmp_path) -> None:
    executable = tmp_path / "fixed-tool"
    digest = _executable(executable, b"#!/bin/sh\nprintf trusted\n")
    descriptor = open_verified_executable(
        executable, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    replacement = tmp_path / "replacement"
    _executable(replacement, b"#!/bin/sh\nprintf attacker\n")
    request = ProcessRequest.fixed(
        argv=(str(executable),),
        cwd=tmp_path,
        timeout_seconds=1,
        output_limit_bytes=1024,
        executable_fd=descriptor,
    )
    os.replace(replacement, executable)
    try:
        outcome = BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert outcome.returncode == 0
    assert outcome.stdout == b"trusted"


def test_verified_descriptor_execution_resists_same_inode_overwrite(tmp_path) -> None:
    executable = tmp_path / "fixed-tool"
    digest = _executable(executable, b"#!/bin/sh\nprintf trusted\n")
    descriptor = open_verified_executable(
        executable, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    executable.write_bytes(b"#!/bin/sh\nprintf attacker\n")
    executable.chmod(0o755)
    request = ProcessRequest.fixed(
        argv=(str(executable),),
        cwd=tmp_path,
        timeout_seconds=1,
        output_limit_bytes=1024,
        executable_fd=descriptor,
    )
    try:
        outcome = BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert outcome.returncode == 0
    assert outcome.stdout == b"trusted"


def test_descriptor_bound_python_tool_imports_only_fixed_common_directory(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    common = bundle / "bin/common"
    common.mkdir(parents=True)
    (common / "fixture_helper.py").write_text("VALUE = 'trusted'\n", encoding="utf-8")
    executable = bundle / "bin/tool.py"
    digest = _executable(
        executable,
        b"#!/usr/bin/env python3\nimport fixture_helper\nprint(fixture_helper.VALUE, end='')\n",
    )
    descriptor = open_verified_executable(
        executable, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    request = ProcessRequest(
        (str(executable),),
        bundle,
        {**FIXED_PROCESS_ENVIRONMENT, "PYTHONPATH": str(common)},
        2,
        1024,
        descriptor,
    )
    try:
        outcome = BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)

    assert outcome.returncode == 0
    assert outcome.stdout == b"trusted"


def test_python_tool_imports_verified_support_snapshot_after_source_path_swap(
    tmp_path,
) -> None:
    policy, _ = installed_policy(tmp_path)
    source = policy.bundle_root / "bin/common/asset_id.py"
    source.write_text("VALUE = 'trusted'\n", encoding="utf-8")
    support = tuple(
        replace(
            item,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            size_bytes=source.stat().st_size,
        )
        if item.relative_path == "bin/common/asset_id.py"
        else item
        for item in policy.support_files
    )
    policy = replace(policy, support_files=support)
    support_archive = open_verified_support_archive(policy)
    executable = tmp_path / "tool.py"
    digest = _executable(
        executable,
        b"#!/usr/bin/env python3\nimport asset_id\nprint(asset_id.VALUE, end='')\n",
    )
    descriptor = open_verified_executable(
        executable, digest, _test_only_allow_unprivileged=True
    )
    assert descriptor is not None
    replacement = tmp_path / "attacker.py"
    replacement.write_text("VALUE = 'attacker'\n", encoding="utf-8")
    os.replace(replacement, source)
    request = ProcessRequest(
        (str(executable),),
        tmp_path,
        {**FIXED_PROCESS_ENVIRONMENT, "PYTHONPATH": f"/proc/self/fd/{support_archive}"},
        2,
        1024,
        descriptor,
        support_archive_fd=support_archive,
    )
    try:
        outcome = BoundedProcessRunner().run(request)
    finally:
        os.close(descriptor)
        os.close(support_archive)

    assert outcome.returncode == 0
    assert outcome.stdout == b"trusted"
