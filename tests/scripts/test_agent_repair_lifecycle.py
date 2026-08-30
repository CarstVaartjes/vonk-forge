from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "packaging/debian/preinst-repair"
HARNESS = ROOT / "tests/nodes/test_agent_upgrade_repair_systemd.sh"
MATRIX = ROOT / "tests/nodes/test_agent_upgrade_repair_matrix.sh"
PROBE_SOURCE = ROOT / "rust/crates/vonk-repair-helper-probe/src/main.rs"
BINARY_REVISION = "a122909feaa3b64d7b15371285e727965c3d7e9a"


def _shell_array(source: str, name: str) -> set[str]:
    match = re.search(
        rf"^\s*{name}=\((?P<body>.*?)\)$", source, re.MULTILINE | re.DOTALL
    )
    assert match is not None
    return set(match.group("body").replace("\\\n", " ").split())


def test_repair_native_harness_covers_every_durable_phase() -> None:
    runner = RUNNER.read_text()
    harness = HARNESS.read_text()
    matrix = MATRIX.read_text()
    production_phases = set(re.findall(r"write_phase ([a-z-]+)", runner))
    accepted_phases = set(
        re.search(r'case "\$next_phase" in\n\s*([^)]*)\)', runner).group(1).split("|")
    )
    harness_phases = _shell_array(harness, "repair_crash_phases")
    boot_crashpoints = _shell_array(harness, "repair_boot_crashpoints")

    assert production_phases == {
        "armed",
        "installing",
        "configured",
        "helper-proven",
        "agent-proven",
    }
    assert production_phases == accepted_phases == harness_phases
    full = re.search(
        r"^\s*full\)\n(?P<body>.*?)^\s*;;$", matrix, re.MULTILINE | re.DOTALL
    )
    assert full is not None
    assert boot_crashpoints == {
        "pre-runner-rename",
        "post-runner-rename",
        "helper-proven-boot",
        "agent-proven-boot",
    }
    assert _shell_array(full.group("body"), "phases") == (
        production_phases | boot_crashpoints | {"none"}
    )
    assert "systemctl --system kill --kill-whom=all --signal=SIGKILL" in harness
    assert 'systemctl --system start "$recovery_unit"' in harness
    assert 'systemctl --system freeze "$helper_unit"' in harness
    assert 'systemctl --system start "$socket_unit"' in harness
    assert 'repair_probe_binary=$(realpath -e -- "$REPAIR_PROBE_BINARY")' in harness
    assert "0:0:755:1" in harness


def _assert_frozen_runtime_and_old_runner() -> None:
    expected = {
        ROOT / "packaging/debian/preinst-repair": (
            "45bc818f088638fccf83fa1a88471c956c3906221d5dd9f48e39593997b4f3b4"
        ),
        ROOT / "packaging/debian/postinst-repair": (
            "551a80895f536f30d041ab8019db1df0fbadd2503cb1715f81299a850f5c28ba"
        ),
        ROOT / "scripts/build-agent-deb": (
            "1d87e67ee919149a77cdb246d6462b7542acaa37f07805b38e78637bcf6498d3"
        ),
    }
    for path, digest in expected.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    available = subprocess.run(
        ["git", "cat-file", "-e", f"{BINARY_REVISION}:packaging/debian/preinst"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if available.returncode != 0:
        pytest.skip("pinned a122 repair source is absent from shallow checkout")
    old_runner = subprocess.run(
        ["git", "show", f"{BINARY_REVISION}:packaging/debian/preinst"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".repair-build." not in old_runner
    assert "vonk-forge-package-upgrade-recover.standard" not in old_runner


def test_repair_native_harness_has_a_byte_and_pid_no_mutation_oracle() -> None:
    harness = HARNESS.read_text()

    assert "snapshot_state()" in harness
    assert 'snapshot_state "$test_root/before"' in harness
    assert 'snapshot_state "$test_root/after"' in harness
    assert 'cmp -s "$test_root/before" "$test_root/after"' in harness
    for evidence in (
        "${db:Status-Abbrev}",
        "MainPID",
        "InvocationID",
        "ControlGroup",
        "/proc/$pid/stat",
        "/proc/$pid/exe",
        "/proc/$pid/cgroup",
        "stat -c %d:%i:%u:%g:%a:%h:%s:%Y:%Z",
    ):
        assert evidence in harness


def test_repair_native_harness_adversarial_matrix_is_complete() -> None:
    harness = HARNESS.read_text()
    matrix = MATRIX.read_text()
    faults = _shell_array(harness, "repair_faults")

    assert {
        "none",
        "wrong-node",
        "config-mode",
        "config-symlink",
        "direct-dpkg",
        "dpkg-iU",
        "dpkg-iF",
        "dpkg-iHR",
        "absent",
        "newer",
        "installed-agent",
        "installed-helper",
        "installed-agent-unit",
        "installed-helper-unit",
        "installed-socket-unit",
        "running-agent",
        "running-helper",
        "cgroup-agent",
        "cgroup-helper",
        "source-intent",
        "source-cache",
        "source-runner",
        "source-unit",
        "source-gate",
        "source-dropin",
        "source-blocker",
        "source-pending",
        "source-lock-busy",
    } == faults
    for fault in faults - {"none"}:
        assert fault in matrix
    full = re.search(
        r"^\s*full\)\n(?P<body>.*?)^\s*;;$", matrix, re.MULTILINE | re.DOTALL
    )
    assert full is not None
    assert _shell_array(full.group("body"), "faults") == faults - {"none"}


def test_repair_native_harness_binds_live_versions_and_helper_mediation() -> None:
    harness = HARNESS.read_text()

    _assert_frozen_runtime_and_old_runner()

    assert "0.1.0~dev.335+g2eaaf4d9b2b5" in harness
    assert "0.1.0~dev.381+ga122909feaa3" in harness
    assert "a122909feaa3b64d7b15371285e727965c3d7e9a" in harness
    assert "spk_2818d189042b4c77aefa7796f4befd23" in harness
    assert harness.count('submit_helper_install "$') == 2
    submit = harness[
        harness.index("submit_helper_install() {") : harness.index(
            "force_dpkg_status() {"
        )
    ]
    assert submit.index('chmod 0600 "$trigger_stage"') < submit.index(
        'mv -f -- "$trigger_stage" "$trigger"'
    )
    assert "ordinary helper upgrade: PASS" in harness
    assert "--repair --json" in harness
    assert "--expected-repair-authority-sha256" in harness
    assert harness.count('scripts/verify-agent-deb" --json') == 3
    prebuild_fixture = harness.index("assert_old_fixture")
    postbuild_fixture = harness.index("assert_old_fixture", prebuild_fixture + 1)
    fault_dispatch = harness.index('case "$fault" in')
    helper_dispatch = harness.index('submit_helper_install "$dispatch_candidate"')
    assert prebuild_fixture < postbuild_fixture < fault_dispatch < helper_dispatch
    assert "source_recovery_nonce)=.*/\\1=<redacted>" in harness
    assert 'test "$(wc -l < "$repair_receipt")" -eq 16' in harness
    assert 'test "$(wc -l < "$helper_receipt")" -eq 10' in harness
    assert "authority_sha256=$authority_sha" in harness
    assert "source_intent_sha256=$source_intent_sha" in harness
    assert 'test "$final_nonce" = "$helper_nonce"' in harness
    assert 'test "$final_helper_pid" = "$helper_receipt_pid"' in harness
    assert 'test "$target_agent_pid" = "$final_agent_pid"' in harness
    assert 'test "$target_helper_pid" = "$final_helper_pid"' in harness


def test_repair_native_cleanup_cannot_arm_before_collision_preflight() -> None:
    harness = HARNESS.read_text()
    preflight = harness.index("if dpkg-query -W vonk-forge-agent")
    first_package_build = harness.index("build_package()")
    destructive_cleanup = harness.index("trap cleanup EXIT")

    assert preflight < destructive_cleanup < first_package_build
    collision = harness[preflight:first_package_build]
    assert "agent repair fixture would collide with host state" in collision
    assert "-e /var/lib/vonk-forge" in collision
    assert "/run/vonk-forge-package-candidates" in collision
    assert "/run/vonk-forge-package-helper" in collision
    assert "/usr/lib/vonk-forge" in collision
    assert "/usr/share/doc/vonk-forge-agent" in collision
    assert "/usr/share/keyrings/vonk-forge-release.pub" in collision
    assert "/lib/systemd/system/vonk-forge-package-helper.service.d" in collision
    assert "getent passwd vonk-agent" in collision
    assert "getent group vonk-agent" in collision
    assert "'^vonk-agent:' /etc/subuid /etc/subgid" in collision
    assert "/var/lib/systemd/linger/vonk-agent" in collision


def test_repair_probe_parser_and_manager_identity_contract_is_closed() -> None:
    probe = PROBE_SOURCE.read_text()
    runner = RUNNER.read_text()

    assert (
        'const PROBE: &str = "/var/lib/dpkg/tmp.ci/vonk-repair-helper.probe";' in probe
    )
    assert 'const SETPRIV: &str = "/usr/bin/setpriv";' in probe
    assert 'const AGENT: &str = "/usr/lib/vonk-forge/vonk-agent";' in probe
    assert (
        'const AGENT_CGROUP: &str = "/system.slice/vonk-forge-agent.service";'
        in probe
    )
    assert 'const HELPER: &str = "/usr/lib/vonk-forge/vonk-agent-helper";' in probe
    assert (
        "const HELPER_CGROUP: &str = "
        '"/system.slice/vonk-forge-package-helper.service";' in probe
    )
    dispatch = probe[probe.index("match command.as_str()") :]
    assert dispatch.count('"check-wrapper" =>') == 1
    assert dispatch.count('"probe-helper" =>') == 1
    assert dispatch.count('"probe-agent" =>') == 1
    assert '_ => Err("unsupported command".to_string())' in dispatch
    assert "if args.len() != 2" in probe
    assert "if args.len() != 9" in probe
    assert "if args.len() != 12" in probe
    assert '|| !is_decimal(&args[7])' in probe
    assert '|| !is_decimal(&args[8])' in probe
    assert '|| (args[9] != "none" && args[9] != args[8])' in probe
    assert ".filter(|number| number.to_string() == value)" in probe
    assert ".is_some_and(|number| number > 1)" in probe
    assert "validate_helper_probe_self()?;" in probe
    assert "validate_agent_probe_self(&args[7], &args[8], &args[9])?;" in probe
    assert 'const CAP_SYS_PTRACE: &str = "0000000000080000";' in probe
    assert 'const AGENT_INHERITABLE_CAPS: &str = "0000000000200000";' in probe
    assert 'const AGENT_BOUNDING_CAPS: &str = "00000000002000c2";' in probe
    assert 'status_value(status, "NoNewPrivs:")? != "1"' in probe
    assert 'status_value(status, "Seccomp:")? != "2"' in probe
    assert "libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NOCTTY" in probe
    assert "held.nlink() != 1" in probe
    assert "held.mode() & 0o7777 != 0o755" in probe
    assert 'name.starts_with(b"security.")' in probe
    assert "if hash_reader(&file)? != expected_sha" in probe
    assert "metadata_identity(&before) != metadata_identity(&after)" in probe
    assert "if values != [expected, expected, expected, expected]" in probe
    assert 'for field in ["CapInh:", "CapPrm:", "CapEff:", "CapBnd:", "CapAmb:"]' in probe
    assert 'for field in ["CapPrm:", "CapEff:", "CapAmb:"]' in probe
    assert 'status_value(&status, "CapInh:")? != AGENT_INHERITABLE_CAPS' in probe
    assert 'status_value(&status, "CapBnd:")? != AGENT_BOUNDING_CAPS' in probe
    assert "if raw != expected" in probe
    assert "let held = File::open(&exe_link)" in probe
    assert "let digest = hash_reader(&held)?;" in probe
    assert "if before != after" in probe
    assert "schema_version=1 nonce=" in probe

    manager = runner[
        runner.index("prove_helper_with_manager() {") : runner.index(
            "write_helper_receipt() {"
        )
    ]
    assert manager.index('probe_unit_absent "$probe_unit"') < manager.index(
        "probe_output=$(/usr/bin/systemd-run"
    )
    assert manager.count('probe_unit_absent "$probe_unit"') == 2
    assert manager.count('probe_unit_absent "$agent_probe_unit"') == 2
    assert (
        "vonk-repair-helper-probe-${probe_authority_prefix}-${probe_nonce}.service"
        in manager
    )
    assert (
        "vonk-repair-agent-probe-${probe_authority_prefix}-${probe_nonce}.service"
        in manager
    )
    assert "--property=CapabilityBoundingSet=" in manager
    assert '"$repair_probe" probe-agent' in manager
    assert "--reuid" not in runner
    assert "--regid" not in runner
    assert '[ "$old_agent_groups" = none ]' in runner
    assert '[ "$old_agent_groups" = "$vonk_agent_gid" ]' in runner
    assert "capture_running_old_metadata || return 1" in manager
    for identity in (
        "old_agent_pid",
        "old_agent_start",
        "old_agent_invocation",
        "old_agent_cgroup",
        "old_agent_groups",
        "old_helper_pid",
        "old_helper_start",
        "old_helper_invocation",
        "old_helper_cgroup",
        "old_boot_id",
    ):
        assert f"probe_{identity.removeprefix('old_')}=${identity}" in manager
        assert (
            f'[ "${identity}" = "$probe_{identity.removeprefix("old_")}" ]' in manager
        )


def test_stopped_source_recovery_accepts_only_collected_or_empty_exact_cgroup() -> None:
    runner = RUNNER.read_text()
    stopped = runner[
        runner.index("unit_is_stopped_and_empty() {") : runner.index(
            "stop_and_quiesce_source_recovery() {"
        )
    ]

    assert "--property=MainPID --property=ControlGroup" in stopped
    assert "--value" not in stopped
    for key in ("LoadState", "ActiveState", "SubState", "MainPID", "ControlGroup"):
        assert 'grep -Ec "^${stopped_key}="' in stopped
        assert f"{key}=" in stopped
    assert '[ -z "$stopped_cgroup" ]' in stopped
    assert '[ "$stopped_cgroup" = "$expected_stopped_cgroup" ]' in stopped
    assert 'expected_cgroup_path="/sys/fs/cgroup$expected_stopped_cgroup"' in stopped
    assert '[ -e "$expected_cgroup_path" ]' in stopped
    assert "grep -Fxq 'populated 0'" in stopped


def test_repair_native_probe_is_ephemeral_and_denied_syscalls_are_exercised() -> None:
    harness = HARNESS.read_text()
    matrix = MATRIX.read_text()

    assert "assert_repair_probe_not_persisted()" in harness
    assert harness.count("assert_repair_probe_not_persisted") == 7
    assert 'test ! -e "/var/lib/dpkg/tmp.ci/$repair_probe_control"' in harness
    assert 'test ! -L "/var/lib/dpkg/tmp.ci/$repair_probe_control"' in harness
    assert '-name "*$repair_probe_control*" -print -quit' in harness
    assert '-n "$probe_info_collision"' in harness
    for syscall in ("ptrace", "process_vm_readv", "socket", "mount"):
        assert f'"{syscall}"' in harness
    assert harness.count("errno != EPERM") == 1
    for status in (
        "CapInh:\\t0000000000000000",
        "CapAmb:\\t0000000000000000",
        "NoNewPrivs:\\t1",
        "Seccomp:\\t2",
    ):
        assert status in harness
    assert '? "0000000000080000" : "0000000000000000"' in harness
    for cap_field in ("CapPrm", "CapEff", "CapBnd"):
        assert f'"{cap_field}:\\t%s\\n"' in harness
    assert "--property=CapabilityBoundingSet=CAP_SYS_PTRACE" in harness
    assert "--property=CapabilityBoundingSet=" in harness
    assert "--property=AmbientCapabilities=" in harness
    assert "--property=SystemCallErrorNumber=EPERM" in harness
    assert '"$native_denials" zero "$agent_uid" "$agent_gid"' in harness
    assert '"$native_probe" probe-agent "${probe_args[@]}"' in harness
    for rejection in (
        "uid|7|0",
        "gid|8|0",
        "groups|9|0",
        "pid|0|$old_helper_pid",
        "start|1|1",
        "boot|5|$wrong_boot",
        "agent-digest|4|$wrong_digest",
        "setpriv-digest|10|$wrong_digest",
        "probe-digest|11|$wrong_digest",
    ):
        assert rejection in harness
    assert "vonk-repair-agent-collision-${collision_nonce}.service" in harness
    assert "probe sandbox denied syscalls: PASS" in harness
    assert "phases=(none" in matrix
