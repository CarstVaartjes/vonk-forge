from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "scripts/build-agent-deb"
VERIFY = ROOT / "scripts/verify-agent-deb"
PREINST = ROOT / "packaging/debian/preinst"
POSTINST = ROOT / "packaging/debian/postinst"
PRERM = ROOT / "packaging/debian/prerm"
RECOVERY_LIFECYCLE = ROOT / "tests/nodes/test_agent_upgrade_recovery_systemd.sh"
DOCKER_FIREWALL = ROOT / "packaging/bin/vonk-forge-docker-firewall"
PACKAGE_BINARIES = ("vonk-agent", "vonk-agent-helper", "oras")
BUILD_DIGEST = "sha256:" + "b" * 64


def _elf_fixture(path: Path, marker: bytes, architecture: str = "linux-arm64") -> None:
    raw = bytearray(384)
    raw[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<H", raw, 16, 2)
    struct.pack_into(
        "<H", raw, 18, {"linux-arm64": 183, "linux-amd64": 62}[architecture]
    )
    raw[64 : 64 + len(marker)] = marker
    identity_marker = f"VONK_AGENT_BUILD_DIGEST={BUILD_DIGEST}".encode()
    raw[128 : 128 + len(identity_marker)] = identity_marker
    semantic_marker = b"VONK_AGENT_SEMANTIC_VERSION=0.1.0"
    raw[256 : 256 + len(semantic_marker)] = semantic_marker
    path.write_bytes(raw)
    path.chmod(0o555)


def _aarch64_fixture(path: Path, marker: bytes) -> None:
    _elf_fixture(path, marker)


def _release_key(path: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", path],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    path.chmod(0o600)


def _build(
    output: Path,
    binaries: Path,
    key: Path,
    version: str = "0.1.0",
    architecture: str = "linux-arm64",
    acceptance_baseline: bool = False,
) -> subprocess.CompletedProcess[str]:
    (binaries / "oras.LICENSE").write_text("ORAS test license\n")
    command = [
            BUILD,
            "--version",
            version,
            "--architecture",
            architecture,
            "--build-digest",
            BUILD_DIGEST,
            "--release-private-key",
            key,
            "--binaries-dir",
            binaries,
            "--source-date-epoch",
            "1786060800",
            "--output-dir",
            output,
        ]
    if acceptance_baseline:
        command.append("--acceptance-baseline")
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_preinst(
    tmp_path: Path,
    *,
    candidate: str,
    arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / f"preinst-{candidate}-{'-'.join(arguments)}"
    script.write_text(PREINST.read_text().replace("@VERSION@", candidate))
    script.chmod(0o755)
    return subprocess.run(
        [script, *arguments],
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "SYSTEMD_OFFLINE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _firewall_config(
    tmp_path: Path, replacement: tuple[str, str] | None = None
) -> Path:
    text = """VONK_NAS_MANAGEMENT_IP=192.168.1.231
VONK_NODE_MANAGEMENT_IP=192.168.1.211
VONK_NODE_FABRIC_IP=192.168.100.10
VONK_PEER_FABRIC_IP=192.168.100.11
VONK_ENDPOINT_HOST_PORTS=8000,8101
VONK_HOST_ENDPOINT_PORTS=8888
VONK_RENDEZVOUS_PORT=29500"""
    if replacement is not None:
        old, new = replacement
        text = text.replace(old, new)
    path = tmp_path / "docker-firewall.conf"
    path.write_text(text + "\n")
    path.chmod(0o600)
    return path


def _allocate_subid_range(
    tmp_path: Path,
    entries: str,
    *,
    minimum: int = 100_000,
    maximum: int = 600_100_000,
    count: int = 65_536,
) -> subprocess.CompletedProcess[str]:
    source = POSTINST.read_text()
    functions = source[source.index("login_value() {") : source.index("sub_uid_min=")]
    script = tmp_path / "allocate-subid-range"
    script.write_text(
        "#!/bin/sh\nset -eu\n"
        + functions
        + '\nallocate_subid_range "$1" "$2" "$3" "$4"\n'
    )
    script.chmod(0o755)
    ranges = tmp_path / "subuid"
    ranges.write_text(entries)
    return subprocess.run(
        [script, ranges, str(minimum), str(maximum), str(count)],
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "arguments",
    (
        ("install",),
        ("install", "0.1.0"),
        ("upgrade", "0.1.0"),
        ("abort-upgrade", "0.1.1"),
    ),
)
def test_preinst_accepts_every_valid_nondowngrade_dpkg_invocation(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    result = _run_preinst(tmp_path, candidate="0.1.1", arguments=arguments)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operation", ("install", "upgrade"))
def test_preinst_refuses_every_direct_package_downgrade_form(
    tmp_path: Path, operation: str
) -> None:
    result = _run_preinst(tmp_path, candidate="0.9.0", arguments=(operation, "1.0.0"))

    assert result.returncode != 0
    assert "refusing downgrade from 1.0.0 to 0.9.0" in result.stderr


@pytest.mark.parametrize(
    ("entries", "expected"),
    (
        ("", "100000-165535"),
        ("carst:100000:65536\n", "165536-231071"),
        ("first:165536:65536\nlast:100000:65536\n", "231072-296607"),
        ("first:100000:32768\nsecond:198304:65536\n", "132768-198303"),
    ),
)
def test_postinst_allocates_first_nonoverlapping_subid_range(
    tmp_path: Path, entries: str, expected: str
) -> None:
    result = _allocate_subid_range(tmp_path, entries)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_postinst_rejects_exhausted_subid_space(tmp_path: Path) -> None:
    result = _allocate_subid_range(
        tmp_path,
        "first:100000:65536\nsecond:165536:65536\n",
        maximum=231071,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_package_manages_the_rootless_podman_user_manager() -> None:
    postinst = POSTINST.read_text()
    prerm = PRERM.read_text()

    assert "/usr/bin/loginctl enable-linger vonk-agent" in postinst
    assert "if [ ! -e /var/lib/systemd/linger/vonk-agent ]; then" in postinst
    assert "/usr/bin/loginctl disable-linger vonk-agent" in prerm


def test_upgrade_postinst_is_local_and_cannot_poison_dpkg_on_controller_failure() -> (
    None
):
    postinst = POSTINST.read_text()

    assert (
        '&& { [ -n "${2:-}" ] || [ "$pending_present" -eq 1 ]; };'
        in postinst
    )
    assert "deb-systemd-invoke restart vonk-forge-agent.service" not in postinst
    helper_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-package-helper.service"
    )
    agent_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-agent.service",
        helper_restart,
    )
    assert helper_restart < agent_restart
    assert "post_restart_self_test" not in postinst
    assert "/run/vonk-forge-agent" not in postinst
    assert "self-test" not in postinst
    for forbidden in (
        "verify-readiness",
        "is-active",
        "readiness.json",
        "/usr/bin/sleep",
        "curl",
        "wget",
    ):
        assert forbidden not in postinst


@pytest.mark.skip(reason="superseded by durable package recovery")
def test_package_helper_upgrade_bridge_is_narrow_bounded_and_retryable() -> None:
    preinst = PREINST.read_text()
    postinst = POSTINST.read_text()
    helper = (
        ROOT / "packaging/systemd/vonk-forge-package-helper.service"
    ).read_text()
    exact_paths = (
        "ReadWritePaths=/usr/share/keyrings "
        "/usr/share/doc/vonk-forge-agent"
    )

    assert exact_paths in helper.splitlines()
    assert "ReadWritePaths=/usr/share" not in helper.splitlines()
    assert "ReadWritePaths=/usr" not in helper.splitlines()
    assert "ProtectSystem=strict" in helper.splitlines()

    assert "inside_package_helper" in preinst
    assert "/proc/self/cgroup" in preinst
    assert "previous-main-pid" in preinst
    assert "vonk-forge-package-helper-upgrade-bridge.service" in preinst
    assert "--property=ActiveState" in preinst
    assert "--property=LoadState" in preinst
    assert "reset-failed" in preinst
    assert "RuntimeMaxSec=180s" in preinst
    assert 'attempts" -lt 1200' in preinst
    assert "bridge_dropin_dir=/lib/systemd/system/" in preinst
    assert "bridge_root=/run/vonk-forge-package-helper/upgrade-bridge" in preinst
    assert "bridge_dropin_dir=/run/systemd/system/" not in preinst
    assert exact_paths in preinst
    assert "stop before dpkg can partially unpack" in preinst
    assert "retry the signed controller upgrade" in preinst
    assert preinst.index("schedule_bridge_restart") < preinst.rindex("exit 1")
    preunpack_gate = preinst.index("write_preunpack_pending ||")
    bridge_decision = preinst.index(
        "# Development packages predating this bridge"
    )
    assert preunpack_gate < bridge_decision
    assert preinst.index('/usr/bin/sync -f "$pending_new"') < preunpack_gate
    assert preinst.index(
        "/usr/bin/sync -f /var/lib/vonk-forge"
    ) < preunpack_gate
    assert preinst.index('package_action=1') < preunpack_gate
    assert preinst.index('inside_helper=0') < preunpack_gate
    assert '[ -n "$old_version" ] || [ "$inside_helper" -eq 1 ]' in preinst

    assert "vonk-forge-package-helper-upgrade-finish.service" in postinst
    assert "--property=ActiveState" in postinst
    assert "--property=LoadState" in postinst
    assert "reset-failed" in postinst
    assert "schedule_permanent_helper_restart" in postinst
    assert exact_paths in postinst
    schedule_call = postinst.index(
        'schedule_permanent_helper_restart "$PPID" "$dpkg_start"'
    )
    assert postinst.rfind(exact_paths, 0, schedule_call) >= 0
    helper_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-package-helper.service"
    )
    agent_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-agent.service",
        helper_restart,
    )
    assert helper_restart < agent_restart < schedule_call
    retire = postinst.index('/usr/bin/mv -- "$bridge_dropin" "$bridge_retired"')
    permanent_reload = postinst.index(
        "/usr/bin/systemctl --system daemon-reload", retire
    )
    cleanup = postinst.index('/usr/bin/rm -f -- "$bridge_retired"')
    assert retire < permanent_reload < cleanup < agent_restart < schedule_call
    assert "RuntimeMaxSec=240s" in postinst
    assert 'attempts" -lt 1200' in postinst
    assert "bridge_dropin_dir=/lib/systemd/system/" in postinst
    assert "bridge_root=/run/vonk-forge-package-helper/upgrade-bridge" in postinst
    assert "ReadWritePaths=/run/systemd/system" not in postinst
    assert "helper_has_effective_bridge_paths" in preinst
    assert "--property=ReadWritePaths --value" in preinst


@pytest.mark.skip(reason="superseded by durable package recovery")
def test_upgrade_bridge_uses_inherited_namespace_not_the_unpacked_unit() -> None:
    preinst = PREINST.read_text()

    assert "helper_unit_has_package_paths" not in preinst
    assert "helper_unit_path=" not in preinst
    assert "helper_namespace_has_package_paths" in preinst
    assert 'keyrings=/usr/share/keyrings' in preinst
    assert 'package_doc=/usr/share/doc/vonk-forge-agent' in preinst
    assert '"$keyrings/.vonk-package-write.XXXXXX"' in preinst
    assert '"$package_doc/.vonk-package-write.XXXXXX"' in preinst
    assert preinst.count("helper_namespace_has_package_paths") >= 3
    acceptance = preinst.index(
        'if [ "$helper_main_pid" != "$previous_main_pid" ]'
    )
    acceptance_end = preinst.index("exit 0", acceptance)
    acceptance_block = preinst[acceptance:acceptance_end]
    assert "bridge_dropin_is_safe" in acceptance_block
    assert "helper_has_effective_bridge_paths" in acceptance_block
    assert "helper_namespace_has_package_paths" in acceptance_block
    assert 'package_action=1\n    old_version=${2:-}' in preinst
    assert 'if [ -n "$old_version" ]; then' in preinst
    assert preinst.index('if [ -n "$old_version" ]; then') < preinst.index(
        "inside_helper=0"
    )
    assert '[ -n "$old_version" ] || [ "$inside_helper" -eq 1 ]' in preinst


@pytest.mark.skip(reason="superseded by durable package recovery")
def test_package_helper_upgrade_bridge_fails_closed_on_unsafe_runtime_state() -> None:
    preinst = PREINST.read_text()
    postinst = POSTINST.read_text()

    for unsafe_guard in (
        '[ -L "$directory" ]',
        '[ -d "$directory" ] && [ ! -L "$directory" ]',
        '[ $((0$mode & 0022)) -eq 0 ]',
        '[ -f "$bridge_dropin" ] && [ ! -L "$bridge_dropin" ]',
        'stat -c %u:%a "$bridge_dropin"',
        '[ -f "$bridge_main_pid" ] && [ ! -L "$bridge_main_pid" ]',
        'stat -c %u:%a "$bridge_main_pid"',
    ):
        assert unsafe_guard in preinst
    assert "cannot stage the package-helper upgrade bridge" in preinst
    assert "cannot schedule the package-helper upgrade bridge" in preinst
    assert "cannot schedule the permanent package finisher" in postinst
    assert '[ ! -L "$helper_unit_path" ]' in postinst
    assert '[ ! -L "$bridge_dropin" ]' in postinst
    assert '[ ! -L "$bridge_main_pid" ]' in postinst


@pytest.mark.skip(reason="superseded by durable package recovery")
def test_upgrade_bridge_only_uses_dev335_writable_mounts() -> None:
    preinst = PREINST.read_text()
    helper = (
        ROOT / "packaging/systemd/vonk-forge-package-helper.service"
    ).read_text()
    helper_socket = (
        ROOT / "packaging/systemd/vonk-forge-package-helper.socket"
    ).read_text()

    assert "/lib/systemd/system" in next(
        line
        for line in helper.splitlines()
        if line.startswith("ReadWritePaths=/var/lib/vonk-forge ")
    ).split()
    assert "DirectoryMode=0711" in helper_socket.splitlines()
    assert "RuntimeDirectory=vonk-forge-package-candidates" in helper.splitlines()
    assert (
        "bridge_dropin_dir=/lib/systemd/system/"
        "vonk-forge-package-helper.service.d"
    ) in preinst.splitlines()
    assert (
        "bridge_root=/run/vonk-forge-package-helper/upgrade-bridge"
        in preinst.splitlines()
    )
    assert "bridge_dropin_dir=/run/systemd/system/" not in preinst
    assert "bridge_root=/run/vonk-forge-package-helper-upgrade-bridge" not in preinst


def test_controller_upgrade_commits_recovery_before_pending_gate() -> None:
    preinst = PREINST.read_text()
    postinst = POSTINST.read_text()
    cache_commit = preinst.index('/usr/bin/mv -- "$cached_new" "$cached_package"')
    runner_commit = preinst.index('atomic_install "$0" "$runner" 555')
    unit_commit = preinst.index("stage_recovery_unit ||")
    agent_gate_commit = preinst.index("stage_agent_gate ||")
    blocker_commit = preinst.index('atomic_text "$agent_blocker" 600')
    intent_commit = preinst.index('atomic_text "$intent" 600')
    service_start = preinst.index('--no-block start "$unit_name"', intent_commit)
    pending_commit = preinst.index('atomic_text "$pending" 600', service_start)

    assert cache_commit < runner_commit < unit_commit < agent_gate_commit
    assert agent_gate_commit < intent_commit < blocker_commit
    assert intent_commit < service_start < pending_commit
    assert 'package_sha256=$package_digest' in preinst
    assert 'recovery_nonce=$recovery_nonce' in preinst
    assert '/usr/bin/sync -f "$state_dir"' in preinst
    assert "durable_recovery=1" in postinst
    assert '[ "$durable_recovery" -ne 1 ]' in postinst


def test_recovery_binds_exact_dev335_dpkg_invocation_and_candidate() -> None:
    preinst = PREINST.read_text()

    assert '"$(/usr/bin/readlink -f "/proc/$PPID/exe")" = /usr/bin/dpkg' in preinst
    assert '= --install' in preinst
    assert '= --force-confold' in preinst
    assert "/var/lib/vonk-forge/incoming/[0-9a-f]*.deb" in preinst
    assert "expected_candidate_metadata=$agent_uid:$agent_gid:600:1" in preinst
    assert "custody_root=/run/vonk-forge-package-candidates" in preinst
    assert '[ "${#invocation}" -eq 32 ]' in preinst
    assert "expected_candidate_metadata=0:0:600:1" in preinst
    assert 'safe_root_directory "$invocation_dir" 700' in preinst
    assert "candidate_before=" in preinst and "candidate_after=" in preinst
    assert "helper_namespace_has_package_paths" in preinst
    assert "durable recovery armed outside the inherited helper sandbox" in preinst
    assert "Bootstrap trust boundary" in preinst
    assert "old-protocol TOCTOU" in preinst


def test_root_custody_lifecycle_executes_the_exact_real_dpkg_contract() -> None:
    lifecycle = RECOVERY_LIFECYCLE.read_text()

    assert "candidate_custody=${CANDIDATE_CUSTODY:-legacy}" in lifecycle
    assert "custody_root=/run/vonk-forge-package-candidates" in lifecycle
    assert "custody_invocation=0123456789abcdef0123456789abcdef" in lifecycle
    assert "candidate=$custody_root/$custody_invocation/$package_digest.deb" in lifecycle
    assert "helper_runtime_directory=vonk-forge-package-candidates" in lifecycle
    assert "helper_runtime_mode=0700" in lifecycle
    assert "helper_runtime_preserve=restart" in lifecycle
    assert 'test "$(stat -c %u:%g:%a "$custody_root")" = 0:0:700' in lifecycle
    assert 'test "$(stat -c %u:%g:%a:%h "$candidate")" = 0:0:600:1' in lifecycle
    assert 'mapfile -d \'\' -t dpkg_argv < "/proc/$dpkg_pid/cmdline"' in lifecycle
    assert 'test "${dpkg_argv[0]}" = /usr/bin/dpkg' in lifecycle
    assert 'test "${dpkg_argv[1]}" = --install' in lifecycle
    assert 'test "${dpkg_argv[2]}" = --force-confold' in lifecycle
    assert 'test "${dpkg_argv[3]}" = "$candidate"' in lifecycle
    assert (
        "ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent"
        in lifecycle.splitlines()
    )
    assert (
        'upgrade_invocations=/var/lib/vonk-forge/upgrade-invocations.$(basename '
        '"$test_root")'
    ) in lifecycle
    assert 'rm -f -- "$upgrade_invocations"' in lifecycle
    assert 'test "$(wc -l < "$upgrade_invocations")" -eq 1' in lifecycle


def test_recovery_lifecycle_collision_check_cannot_remove_host_state() -> None:
    lifecycle = RECOVERY_LIFECYCLE.read_text()

    preflight = lifecycle.index("if dpkg-query -W vonk-forge-agent")
    destructive_cleanup = lifecycle.index("trap cleanup EXIT")
    assert lifecycle.index("trap cleanup_test_root EXIT") < preflight
    assert preflight < destructive_cleanup
    collision_check = lifecycle[preflight:destructive_cleanup]
    assert 'systemctl --system cat "$agent_unit"' in collision_check
    assert "-L /run/vonk-forge-package-candidates" in collision_check
    for protected_path in (
        "/var/lib/vonk-forge/package-upgrade",
        "/var/lib/vonk-forge/helper-upgrade.pending",
        "/var/lib/vonk-forge/helper-upgrade.receipt",
        "vonk-forge-agent.service.d/20-package-upgrade-recovery.conf",
        "vonk-forge-package-helper.socket.d",
    ):
        assert protected_path in collision_check


def test_recovery_lifecycle_crash_point_is_race_safe_and_diagnostic() -> None:
    lifecycle = RECOVERY_LIFECYCLE.read_text()

    baseline = lifecycle.index(
        'SYSTEMD_OFFLINE=1 dpkg --unpack --force-confold "$baseline_package"'
    )
    start_old_helper = lifecycle.index('systemctl --system start "$helper_unit"')
    restore_new_unit = lifecycle.index(
        'install -o root -g root -m 0644 "$test_root/installed-helper.service"'
    )
    stage_candidate = lifecycle.index("stage_candidate", restore_new_unit)
    assert baseline < start_old_helper < restore_new_unit < stage_candidate

    for historical_line in (
        "BindReadOnlyPaths=-/run/docker.sock "
        "-/run/vonk-forge-agent/runtime-requests "
        "-/var/lib/vonk-forge-agent/image-imports",
        "ReadWritePaths=-/var/lib/vonk-forge-agent/models "
        "-/var/lib/vonk-forge-agent/runs "
        "-/var/lib/vonk-forge-agent/run-metadata",
        "TimeoutStartSec=30s",
        "TimeoutStopSec=15s",
        "KillMode=mixed",
    ):
        assert historical_line in lifecycle.splitlines()

    assert 'crash_pending_kind=stale' in lifecycle
    assert 'crash_pending_kind=normalized' in lifecycle
    assert '"$test_root/crash-point-pending"' in lifecycle
    assert 'cmp -s "$test_root/normalized-pending"' in lifecycle
    assert "trap thaw_helper EXIT" in lifecycle
    assert 'trap \'exit 129\' HUP' in lifecycle
    assert 'trap \'exit 130\' INT' in lifecycle
    assert 'trap \'exit 143\' TERM' in lifecycle
    assert "dump_failure_diagnostics" in lifecycle
    assert "journalctl --system --no-pager -n 200" in lifecycle
    assert "recovery_nonce)=.*/\\1=<redacted>" in lifecycle


def test_recovery_is_static_offline_named_only_and_compare_deletes() -> None:
    preinst = PREINST.read_text()
    socket = (ROOT / "packaging/systemd/vonk-forge-package-helper.socket").read_text()
    recovery = (
        ROOT / "packaging/systemd/vonk-forge-package-upgrade-recover.service"
    ).read_text()
    agent_gate = (
        ROOT
        / "packaging/systemd/vonk-forge-agent.service.d/"
        "20-package-upgrade-recovery.conf"
    ).read_text()

    assert "Wants=vonk-forge-package-upgrade-recover.service" in socket
    assert "ConditionPathExists=/var/lib/vonk-forge/package-upgrade/intent" in recovery
    assert "ExecCondition=+" in agent_gate
    assert "allow-agent-start" in agent_gate
    normalize = preinst.index("normalize_pending_gate ||")
    wait = preinst.index("wait_for_original_dpkg ||", normalize)
    stop_agent = preinst.index('stop "$agent_unit"', wait)
    assert normalize < wait < stop_agent
    assert "safe_known_pending" in preinst
    assert "state=pre-unpack|helper_sha256=" in preinst
    blocker_retire = preinst.index('/usr/bin/rm -- "$agent_blocker"')
    agent_restart = preinst.index('restart "$agent_unit"', blocker_retire)
    intent_retire = preinst.index('/usr/bin/rm -- "$intent"', agent_restart)
    assert blocker_retire < agent_restart < intent_retire
    assert "IPAddressDeny=any" in recovery
    assert 'dpkg --force-confold --configure "$package_name"' in preinst
    assert 'dpkg --install --force-confold "$cached_package"' in preinst
    assert "dpkg --configure -a" not in preinst
    assert "apt-get" not in preinst and "curl" not in preinst
    assert preinst.count("intent_snapshot") >= 5
    assert "intent changed before gate retirement" in preinst
    assert "intent changed before retirement" in preinst
    assert '"/proc/$service_pid/exe"' in preinst
    assert 'for bridge_file in "$legacy_bridge" "$legacy_bridge_retired"' in preinst
    assert "retire_legacy_bridge || unsafe_state" in preinst


def test_upgrade_finisher_budget_covers_slow_agent_and_helper_stops() -> None:
    postinst = POSTINST.read_text()
    agent_unit = (ROOT / "packaging/systemd/vonk-forge-agent.service").read_text()
    helper_unit = (
        ROOT / "packaging/systemd/vonk-forge-package-helper.service"
    ).read_text()
    agent_timeout = int(
        next(
            line.removeprefix("TimeoutStopSec=").removesuffix("s")
            for line in agent_unit.splitlines()
            if line.startswith("TimeoutStopSec=")
        )
    )
    helper_timeout = int(
        next(
            line.removeprefix("TimeoutStopSec=").removesuffix("s")
            for line in helper_unit.splitlines()
            if line.startswith("TimeoutStopSec=")
        )
    )

    assert agent_timeout == 30
    assert helper_timeout == 15
    assert "RuntimeMaxSec=240s" in postinst
    assert 'attempts" -lt 1200' in postinst
    helper_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-package-helper.service"
    )
    agent_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-agent.service",
        helper_restart,
    )
    assert helper_restart < agent_restart
    assert 240 >= 120 + helper_timeout + agent_timeout + 60


def test_upgrade_finisher_causally_proves_helper_before_agent_identity() -> None:
    postinst = POSTINST.read_text()

    pending_write = postinst.index('"helper_sha256=$helper_digest"')
    helper_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-package-helper.service"
    )
    running_digest = postinst.index('"/proc/$new_helper_pid/exe"', helper_restart)
    installed_digest = postinst.index(
        "/usr/lib/vonk-forge/vonk-agent-helper", running_digest
    )
    receipt_write = postinst.index('"helper_main_pid=$new_helper_pid"')
    receipt_file_sync = postinst.index(
        '/usr/bin/sync -f "$receipt_new"', receipt_write
    )
    receipt_rename = postinst.index(
        '/usr/bin/mv -f -- "$receipt_new" "$helper_receipt"', receipt_file_sync
    )
    receipt_dir_sync = postinst.index(
        "/usr/bin/sync -f /var/lib/vonk-forge", receipt_rename
    )
    confirmed_digest = postinst.index("confirmed_helper_digest=", receipt_dir_sync)
    pending_retire = postinst.index(
        '/usr/bin/rm -f -- "$helper_pending"', confirmed_digest
    )
    pending_retire_sync = postinst.index(
        "/usr/bin/sync -f /var/lib/vonk-forge", pending_retire
    )
    agent_restart = postinst.index(
        "/usr/bin/systemctl --system restart \\\n                vonk-forge-agent.service",
        helper_restart,
    )
    schedule_call = postinst.index(
        'schedule_permanent_helper_restart "$PPID" "$dpkg_start"'
    )

    assert pending_write < schedule_call
    assert helper_restart < running_digest < installed_digest < receipt_write
    assert receipt_write < receipt_file_sync < receipt_rename < receipt_dir_sync
    assert receipt_dir_sync < confirmed_digest < pending_retire
    assert pending_retire < pending_retire_sync < agent_restart < schedule_call
    assert "helper-upgrade.pending" in postinst
    assert "helper-upgrade.receipt" in postinst
    assert '"schema_version=1"' in postinst
    assert '"activated_at=$activated_at"' in postinst
    assert "/usr/bin/date -u +%Y-%m-%dT%H:%M:%SZ" in postinst
    assert "ReadWritePaths=/var/lib/vonk-forge" in postinst
    pending_file_sync = postinst.index('/usr/bin/sync -f "$pending_new"')
    pending_rename = postinst.index(
        '/usr/bin/mv -f -- "$pending_new" "$helper_pending"'
    )
    pending_dir_sync = postinst.index(
        "/usr/bin/sync -f /var/lib/vonk-forge", pending_rename
    )
    assert pending_file_sync < pending_rename < pending_dir_sync < schedule_call
    assert "^version=[0-9A-Za-z.+~:-]+$" in postinst
    assert "active package helper identity is invalid" in postinst


def test_interrupted_upgrade_state_has_safe_fresh_recovery_and_remove_paths() -> None:
    postinst = POSTINST.read_text()
    prerm = PRERM.read_text()

    assert "clear_stale_pending_for_fresh_install" not in postinst
    pending_probe = postinst.index(
        'if [ -e "$helper_pending" ] || [ -L "$helper_pending" ]'
    )
    finisher_decision = postinst.index(
        '&& { [ -n "${2:-}" ] || [ "$pending_present" -eq 1 ]; };'
    )
    pending_write = postinst.index("pending_digests=$(write_helper_pending)")
    schedule = postinst.index(
        'schedule_permanent_helper_restart "$PPID" "$dpkg_start"'
    )
    assert pending_probe < finisher_decision < pending_write < schedule
    assert "interrupted helper activation requires a live systemd configure retry" in postinst
    assert "pending helper upgrade state is unsafe" in postinst

    stop_finisher = prerm.index(
        "vonk-forge-package-helper-upgrade-finish.service"
    )
    refuse_durable = prerm.index("cannot remove package during durable upgrade recovery")
    stop_agent = prerm.index(
        "deb-systemd-invoke stop vonk-forge-agent.service"
    )
    stop_helper = prerm.index(
        "deb-systemd-invoke stop vonk-forge-package-helper.service"
    )
    remove_evidence = prerm.index('/usr/bin/rm -f -- "$pending" "$receipt"')
    assert refuse_durable < stop_finisher < stop_agent < stop_helper < remove_evidence
    assert "safe_pending" in prerm
    assert "safe_receipt" in prerm
    assert "schema_version=2" in prerm
    assert "intent_sha256=" in prerm
    assert "schema_version=2" in postinst
    assert "intent_sha256=" in postinst
    assert "/usr/bin/sync -f /var/lib/vonk-forge" in prerm


def test_postinst_creates_stable_root_owned_native_machine_evidence() -> None:
    postinst = POSTINST.read_text()

    assert "umask 077" in postinst
    assert "machine_evidence=/var/lib/vonk-forge-agent/machine-evidence" in postinst
    assert "openssl rand -hex 32" in postinst
    assert "install -o root -g vonk-agent -m 0640" in postinst
    assert "ssh_host_" not in postinst


@pytest.mark.parametrize(
    ("package_version", "expected_semantic"),
    (
        ("0.1.0", "0.1.0"),
        ("0.1.0~dev.418+g0123456789ab", "0.1.0"),
        ("0.1.0+lifecycle.1", "0.1.0"),
        ("0.0.0~acceptance.1+g0123456789ab", "0.0.0"),
    ),
)
def test_postinst_derives_the_exact_cargo_semantic_version(
    tmp_path: Path, package_version: str, expected_semantic: str
) -> None:
    source = POSTINST.read_text().replace("@VERSION@", package_version)
    prefix = source[: source.index('if [ "${1:-}" != configure ]')]
    script = tmp_path / "postinst-version"
    script.write_text(prefix + "printf '%s\\n' \"$agent_semver\"\n")
    script.chmod(0o755)

    result = subprocess.run(
        [script], capture_output=True, text=True, check=False, env={"PATH": "/bin"}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected_semantic}\n"


def test_builder_produces_reproducible_verified_arm64_deb(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_result = _build(first, binaries, key)
    second_result = _build(second, binaries, key)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    package_name = "vonk-forge-agent_0.1.0_arm64.deb"
    first_deb = first / package_name
    second_deb = second / package_name
    assert first_deb.read_bytes() == second_deb.read_bytes()
    assert stat.S_IMODE(first_deb.stat().st_mode) == 0o644
    sidecar = (first / f"{package_name}.sha256").read_text().strip()
    assert (
        sidecar
        == f"{hashlib.sha256(first_deb.read_bytes()).hexdigest()}  {package_name}"
    )
    package_signature = (first / f"{package_name}.host.sig").read_text().strip()
    assert len(package_signature) == 128
    assert all(character in "0123456789abcdef" for character in package_signature)
    public_key = tmp_path / "release.pub"
    signature = tmp_path / "package.sig"
    claims = tmp_path / "package.claims"
    subprocess.run(
        ["/usr/bin/openssl", "pkey", "-in", key, "-pubout", "-out", public_key],
        check=True,
    )
    signature.write_bytes(bytes.fromhex(package_signature))
    claims.write_bytes(
        b"VONK-HOST-ARTIFACT-V1\x00deb\x00"
        + hashlib.sha256(first_deb.read_bytes()).digest()
    )
    verified_signature = subprocess.run(
        [
            "/usr/bin/openssl",
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            public_key,
            "-rawin",
            "-in",
            claims,
            "-sigfile",
            signature,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified_signature.returncode == 0, verified_signature.stderr

    verified = subprocess.run(
        [VERIFY, "--json", first_deb],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout
    assert '"ok": true' in verified.stdout

    control = tmp_path / "control"
    extracted = subprocess.run(
        ["/usr/bin/dpkg-deb", "--control", first_deb, control],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extracted.returncode == 0, extracted.stderr
    postinst = (control / "postinst").read_text()
    preinst = (control / "preinst").read_text()
    assert "python" not in postinst
    assert "curl" not in postinst
    assert "wget" not in postinst
    assert "target_version='0.1.0'" in preinst
    assert os.access(control / "preinst", os.X_OK)
    fields = subprocess.run(
        ["/usr/bin/dpkg-deb", "--field", first_deb, "Depends"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "curl" in fields
    assert "podman" in fields
    assert "util-linux" in fields
    assert "uidmap" in fields
    assert "iptables" in fields
    assert "iproute2" in fields
    payload = tmp_path / "payload"
    subprocess.run(["/usr/bin/dpkg-deb", "--extract", first_deb, payload], check=True)
    assert (payload / "etc/vonk-forge-agent/containers-storage.conf").is_file()
    assert not (payload / "etc/vonk-forge-agent/agent.toml").exists()
    assert (control / "conffiles").read_text().splitlines() == [
        "/etc/vonk-forge-agent/containers-storage.conf"
    ]
    agent = payload / "usr/lib/vonk-forge/vonk-agent"
    assert agent.is_file()
    assert stat.S_IMODE(agent.stat().st_mode) == 0o555
    recovery_runner = (
        payload / "usr/lib/vonk-forge/vonk-forge-package-upgrade-recover"
    )
    assert recovery_runner.read_bytes() == (control / "preinst").read_bytes()
    assert stat.S_IMODE(recovery_runner.stat().st_mode) == 0o555
    oras = payload / "usr/lib/vonk-forge/oras"
    assert oras.is_file()
    assert stat.S_IMODE(oras.stat().st_mode) == 0o555
    oras_license = payload / "usr/share/doc/vonk-forge-agent/oras-LICENSE"
    assert oras_license.read_text() == "ORAS test license\n"
    assert stat.S_IMODE(oras_license.stat().st_mode) == 0o644
    cyclone = json.loads(
        (payload / "usr/share/doc/vonk-forge-agent/sbom.cdx.json").read_text()
    )
    oras_component = next(
        component
        for component in cyclone["components"]
        if component.get("name") == "oras"
    )
    assert oras_component["version"] == "1.3.2"
    assert oras_component["hashes"] == [
        {"alg": "SHA-256", "content": hashlib.sha256(oras.read_bytes()).hexdigest()}
    ]
    assert not (payload / "usr/lib/vonk-forge/release/vonk-agent").exists()
    assert not (payload / "usr/lib/vonk-forge/vonk-agent-supervisor").exists()
    assert not (
        payload / "lib/systemd/system/vonk-forge-agent-supervisor.service"
    ).exists()
    assert not (payload / "var/lib/vonk-forge/slots").exists()
    assert not (payload / "usr/bin/vonk-agent-upgrade").exists()
    unit = (payload / "lib/systemd/system/vonk-forge-agent.service").read_text()
    assert (
        "ExecStart=/usr/lib/vonk-forge/vonk-agent "
        "--config /etc/vonk-forge-agent/agent.toml run"
    ) in unit.splitlines()
    assert "supervisor" not in unit
    assert "slot" not in unit
    assert "activation-challenge" not in unit
    assert "Environment=HOME=/var/lib/vonk-forge-agent" in unit
    assert "Environment=XDG_RUNTIME_DIR=/run/vonk-forge-agent" in unit
    assert "ProtectControlGroups=yes" in unit
    # Rootless runc must set the hostname inside each build's private UTS
    # namespace. The dedicated service user still has no ambient host
    # capability, so ProtectHostname would only break container creation.
    assert "ProtectHostname=no" in unit
    assert "ProtectHome=no" in unit
    assert "InaccessiblePaths=/home /root -/run/docker.sock" in unit
    assert "BindReadOnlyPaths=/run/user" not in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in unit
    assert (
        "ReadWritePaths=/var/lib/vonk-forge-agent /var/lib/vonk-forge/incoming "
        "/run/vonk-forge-agent"
    ) in unit
    assert "RestrictNamespaces=user mnt pid ipc uts cgroup net" in unit
    assert "ProtectProc=default" in unit
    assert "ProcSubset=all" in unit
    assert "DeviceAllow=/dev/fuse rw" in unit
    assert "DeviceAllow=char-231:* rw" in unit
    assert "BindPaths=-/dev/fuse" in unit
    assert "Delegate=yes" in unit
    assert "RestrictSUIDSGID=yes" not in unit
    assert "NoNewPrivileges=no" in unit
    assert (
        "CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_SETGID CAP_SETUID CAP_SYS_ADMIN"
        in unit
    )
    helper_socket = (
        payload / "lib/systemd/system/vonk-forge-package-helper.socket"
    ).read_text()
    assert (
        "ListenStream=/run/vonk-forge-package-helper/package-helper.sock"
        in helper_socket.splitlines()
    )
    assert "DirectoryMode=0711" in helper_socket.splitlines()
    firewall = payload / "usr/lib/vonk-forge/vonk-forge-docker-firewall"
    assert firewall.is_file()
    assert os.access(firewall, os.X_OK)
    firewall_unit = (
        payload / "lib/systemd/system/vonk-forge-docker-firewall.service"
    ).read_text()
    assert "Requires=docker.service" in firewall_unit
    assert "PartOf=docker.service" in firewall_unit
    assert "Before=vonk-forge-package-helper.service" in firewall_unit
    assert "CapabilityBoundingSet=CAP_NET_ADMIN" in firewall_unit
    assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK" in firewall_unit
    assert "PrivateNetwork=yes" not in firewall_unit
    helper_unit = (
        payload / "lib/systemd/system/vonk-forge-package-helper.service"
    ).read_text()
    assert "Requires=vonk-forge-docker-firewall.service" in helper_unit
    assert (
        "After=" in helper_unit and "vonk-forge-docker-firewall.service" in helper_unit
    )
    assert "PrivateNetwork=yes" not in helper_unit
    assert "IPAddressDeny=any" in helper_unit
    assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK" in helper_unit
    assert "RuntimeDirectory=vonk-forge-package-candidates" in helper_unit.splitlines()
    assert "RuntimeDirectoryMode=0700" in helper_unit.splitlines()
    assert "RuntimeDirectoryPreserve=restart" in helper_unit.splitlines()
    assert "RuntimeDirectory=vonk-forge-package-helper" not in helper_unit.splitlines()
    assert (
        "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_FSETID "
        "CAP_NET_ADMIN CAP_SETGID CAP_SETUID"
    ) in helper_unit.splitlines()
    assert "AF_INET" not in helper_unit
    assert (
        "ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent"
        in helper_unit.splitlines()
    )
    assert "ReadWritePaths=/usr/share" not in helper_unit.splitlines()
    assert "ReadWritePaths=/usr" not in helper_unit.splitlines()
    assert "usermod --add-subuids" in postinst
    assert "usermod --add-subgids" in postinst
    assert "SUB_UID_MIN" in postinst
    assert "SUB_GID_MIN" in postinst
    assert "sed -i '/^vonk-agent:/d' /etc/subuid" not in postinst
    assert "sed -i '/^vonk-agent:/d' /etc/subgid" not in postinst
    assert "supervisor" not in postinst
    assert "/var/lib/vonk-forge/slots" not in postinst
    assert (
        "ignore_chown_errors"
        not in (payload / "etc/vonk-forge-agent/containers-storage.conf").read_text()
    )


@pytest.mark.parametrize(
    ("architecture", "debian_architecture", "machine"),
    (("linux-arm64", "arm64", 183), ("linux-amd64", "amd64", 62)),
)
def test_builder_and_verifier_make_each_architecture_a_real_package_output(
    tmp_path: Path,
    architecture: str,
    debian_architecture: str,
    machine: int,
) -> None:
    binaries = tmp_path / architecture
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _elf_fixture(binaries / name, name.encode(), architecture)
    key = tmp_path / f"{architecture}.pem"
    _release_key(key)
    output = tmp_path / f"dist-{architecture}"

    built = _build(output, binaries, key, architecture=architecture)

    assert built.returncode == 0, built.stderr
    package = output / f"vonk-forge-agent_0.1.0_{debian_architecture}.deb"
    assert package.is_file()
    verified = subprocess.run(
        [VERIFY, "--json", package],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout
    assert (
        subprocess.run(
            ["/usr/bin/dpkg-deb", "--field", package, "Architecture"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == debian_architecture
    )
    payload = tmp_path / f"payload-{architecture}"
    subprocess.run(["/usr/bin/dpkg-deb", "--extract", package, payload], check=True)
    for name in PACKAGE_BINARIES:
        raw = (payload / f"usr/lib/vonk-forge/{name}").read_bytes()
        assert struct.unpack_from("<H", raw, 18)[0] == machine


def test_builder_enforces_cargo_and_package_semantic_version_consistency(
    tmp_path: Path,
) -> None:
    cargo_version = tomllib.loads((ROOT / "Cargo.toml").read_text())["workspace"][
        "package"
    ]["version"]
    mismatched = "99.0.0" if cargo_version != "99.0.0" else "98.0.0"
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)

    result = _build(tmp_path / "dist", binaries, key, mismatched)

    assert result.returncode == 2
    assert "does not match Cargo semantic version" in result.stderr


def test_builder_allows_only_an_explicit_lower_acceptance_baseline(
    tmp_path: Path,
) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    agent = binaries / "vonk-agent"
    agent.chmod(0o755)
    agent.write_bytes(
        agent.read_bytes().replace(
            b"VONK_AGENT_SEMANTIC_VERSION=0.1.0",
            b"VONK_AGENT_SEMANTIC_VERSION=0.0.0",
        )
    )
    agent.chmod(0o555)
    key = tmp_path / "release.pem"
    _release_key(key)
    version = "0.0.0~acceptance.1+g0123456789ab"

    result = _build(
        tmp_path / "dist",
        binaries,
        key,
        version,
        acceptance_baseline=True,
    )

    assert result.returncode == 0, result.stderr
    package = tmp_path / f"dist/vonk-forge-agent_{version}_arm64.deb"
    assert package.is_file()
    assert (
        subprocess.run(
            ["/usr/bin/dpkg-deb", "--field", package, "Version"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == version
    )
    verified = subprocess.run(
        [VERIFY, "--json", package],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout
    assert json.loads(verified.stdout) == {
        "architecture": "arm64",
        "ok": True,
        "package": "vonk-forge-agent",
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "version": version,
    }


def test_builder_rejects_a_build_digest_that_is_not_embedded_in_the_agent(
    tmp_path: Path,
) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)

    result = subprocess.run(
        [
            BUILD,
            "--version",
            "0.1.0",
            "--architecture",
            "linux-arm64",
            "--build-digest",
            "sha256:" + "c" * 64,
            "--release-private-key",
            key,
            "--binaries-dir",
            binaries,
            "--source-date-epoch",
            "1786060800",
            "--output-dir",
            tmp_path / "dist",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "embedded build digest" in result.stderr


def test_builder_rejects_an_agent_with_a_different_embedded_semantic_version(
    tmp_path: Path,
) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    agent = binaries / "vonk-agent"
    agent.chmod(0o755)
    agent.write_bytes(
        agent.read_bytes().replace(
            b"VONK_AGENT_SEMANTIC_VERSION=0.1.0",
            b"VONK_AGENT_SEMANTIC_VERSION=9.9.9",
        )
    )
    agent.chmod(0o555)
    key = tmp_path / "release.pem"
    _release_key(key)

    result = _build(tmp_path / "dist", binaries, key)

    assert result.returncode == 2
    assert "embedded semantic version" in result.stderr


def test_docker_firewall_rejects_missing_site_configuration(tmp_path: Path) -> None:
    result = subprocess.run(
        [DOCKER_FIREWALL, "--config", tmp_path / "missing.conf", "check"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "site configuration" in result.stderr


def test_docker_firewall_requires_explicit_host_endpoint_authority(
    tmp_path: Path,
) -> None:
    config = _firewall_config(tmp_path)

    accepted = subprocess.run(
        [DOCKER_FIREWALL, "--config", config, "check-host-port", "8888"],
        capture_output=True,
        text=True,
        check=False,
    )
    rejected = subprocess.run(
        [DOCKER_FIREWALL, "--config", config, "check-host-port", "9999"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert accepted.returncode != 64
    assert rejected.returncode != 0
    assert "not authorized" in rejected.stderr


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        ("192.168.1.211", "192.168.001.211", "invalid IPv4"),
        ("8000,8101", "8000,8000", "must be unique"),
        ("VONK_RENDEZVOUS_PORT=29500", "VONK_RENDEZVOUS_PORT=8000", "must differ"),
        (
            "VONK_ENDPOINT_HOST_PORTS=8000,8101",
            "VONK_UNKNOWN=1",
            "unknown or malformed",
        ),
    ),
)
def test_docker_firewall_rejects_noncanonical_site_policy(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    config = _firewall_config(tmp_path, (old, new))

    result = subprocess.run(
        [DOCKER_FIREWALL, "--config", config, "check"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert message in result.stderr


def test_verifier_rejects_tampered_release_sidecar(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    output = tmp_path / "dist"
    result = _build(output, binaries, key)
    assert result.returncode == 0, result.stderr
    deb = output / "vonk-forge-agent_0.1.0_arm64.deb"
    (output / f"{deb.name}.sha256").write_text(f"{'0' * 64}  {deb.name}\n")

    verified = subprocess.run(
        [VERIFY, "--json", deb],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert verified.returncode == 1
    assert "sidecar is invalid" in verified.stdout


def test_verifier_rejects_tampered_host_package_signature(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    output = tmp_path / "dist"
    result = _build(output, binaries, key)
    assert result.returncode == 0, result.stderr
    deb = output / "vonk-forge-agent_0.1.0_arm64.deb"
    (output / f"{deb.name}.host.sig").write_text(f"{'0' * 128}\n")

    verified = subprocess.run(
        [VERIFY, "--json", deb],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert verified.returncode == 1
    assert "required command rejected input" in verified.stdout


def test_builder_accepts_cargo_hardlinked_release_binary(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    # Cargo can materialize release outputs as hardlinks on the runner's
    # filesystem. The builder must bind and verify the inode, not reject a
    # valid native build because its link count is greater than one.
    alias = tmp_path / "cargo-artifact-cache.bin"
    os.link(binaries / "vonk-agent", alias)
    key = tmp_path / "release.pem"
    _release_key(key)

    result = _build(tmp_path / "dist", binaries, key)

    assert result.returncode == 0, result.stderr


def test_builder_rejects_symlinked_release_key(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    linked_key = tmp_path / "linked-release.pem"
    linked_key.symlink_to(key)

    result = _build(tmp_path / "dist", binaries, linked_key)

    assert result.returncode == 2
    assert "private key permissions are unsafe" in result.stderr


def test_builder_accepts_exact_derived_development_version(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    version = "0.1.0~dev.1786300000+g0123456789ab"

    result = _build(tmp_path / "dist", binaries, key, version)

    assert result.returncode == 0, result.stderr
    package = tmp_path / "dist" / f"vonk-forge-agent_{version}_arm64.deb"
    assert package.is_file()
    verified = subprocess.run(
        [VERIFY, "--json", package],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr


@pytest.mark.parametrize(
    "version",
    (
        "0.1.0-rc.1",
        "0.1.0+build.1",
        "0.1.0~devX1786300000+g0123456789ab",
        "0.1.0~dev.1786300000+g0123456789abc",
        "0.1.0~dev.1786300000+g0123456789AB",
        "0.1.0~dev.4102444801+g0123456789ab",
    ),
)
def test_builder_rejects_noncanonical_package_versions(
    tmp_path: Path, version: str
) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)

    result = _build(tmp_path / "dist", binaries, key, version)

    assert result.returncode == 2
    assert "version is not canonical package version" in result.stderr
