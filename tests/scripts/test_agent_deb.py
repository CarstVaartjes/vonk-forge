from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import stat
import struct
import subprocess
import tarfile
import tomllib
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

pytestmark = [pytest.mark.linux_only, pytest.mark.needs_dpkg_deb]

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "scripts/build-agent-deb"
VERIFY = ROOT / "scripts/verify-agent-deb"
PREINST = ROOT / "packaging/debian/preinst"
POSTINST = ROOT / "packaging/debian/postinst"
PRERM = ROOT / "packaging/debian/prerm"
POSTRM = ROOT / "packaging/debian/postrm"
RECOVERY_LIFECYCLE = ROOT / "tests/nodes/test_agent_upgrade_recovery_systemd.sh"
DOCKER_FIREWALL = ROOT / "packaging/bin/vonk-forge-docker-firewall"
PACKAGE_BINARIES = (
    "vonk-agent",
    "vonk-monitor",
    "vonk-agent-helper",
    "vonk-build-egress",
    "vonk-runtime-probe",
    "oras",
)
BUILD_DIGEST = "sha256:" + "b" * 64


def _load_verifier_module():
    loader = SourceFileLoader("test_verify_agent_deb_module", str(VERIFY))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_builder_module():
    loader = SourceFileLoader("test_build_agent_deb_module", str(BUILD))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


BUILD_MODULE = _load_builder_module()
VERIFY_MODULE = _load_verifier_module()


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
    semantic_marker = b"VONK_AGENT_SEMANTIC_VERSION=0.1.1"
    raw[256 : 256 + len(semantic_marker)] = semantic_marker
    if path.name in {"vonk-build-egress", "vonk-runtime-probe"}:
        struct.pack_into("<Q", raw, 32, 320)
        struct.pack_into("<H", raw, 54, 56)
        struct.pack_into("<H", raw, 56, 1)
        struct.pack_into("<I", raw, 320, 1)
    path.write_bytes(raw)
    path.chmod(0o555)


def _aarch64_fixture(path: Path, marker: bytes) -> None:
    _elf_fixture(path, marker)


def test_build_egress_release_binary_must_be_static(tmp_path: Path) -> None:
    path = tmp_path / "vonk-build-egress"
    _elf_fixture(path, b"proxy")
    raw = bytearray(path.read_bytes())
    struct.pack_into("<I", raw, 320, 3)
    path.chmod(0o755)
    path.write_bytes(raw)
    path.chmod(0o555)

    before = path.read_bytes()
    with pytest.raises(BUILD_MODULE.BuildError):
        BUILD_MODULE.read_release_binary(path, machine=183, require_static=True)
    assert path.read_bytes() == before
    path.chmod(0o755)
    _elf_fixture(path, b"proxy")
    accepted = BUILD_MODULE.read_release_binary(path, machine=183, require_static=True)
    assert accepted == path.read_bytes()


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
    version: str = "0.1.1",
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
def test_preinst_installs_a_requested_signed_downgrade(
    tmp_path: Path, operation: str
) -> None:
    # Catches a version-ordering refusal that strands a node on a newer build
    # after the Controller explicitly requested an older signed package.
    result = _run_preinst(tmp_path, candidate="0.9.0", arguments=(operation, "1.0.0"))

    assert result.returncode == 0, result.stderr


def test_preinst_recover_without_intent_is_a_successful_noop(tmp_path: Path) -> None:
    # Catches a recovery runner that parks the unit in a terminal failure
    # (exit 78) once a newer install or a prior run already retired the intent.
    if Path("/var/lib/vonk-forge/package-upgrade/intent").exists():
        pytest.skip("host has a live package upgrade intent")
    result = _run_preinst(tmp_path, candidate="0.1.1", arguments=("recover",))

    assert result.returncode == 0, result.stderr
    assert "unsafe package recovery state" not in result.stderr


def _run_discard_own_state(
    tmp_path: Path, script: Path, target: Path
) -> subprocess.CompletedProcess[str]:
    source = script.read_text()
    start = source.index("discard_own_state() {")
    end = source.index("\n}\n", start) + 3
    runner = tmp_path / f"discard-{script.name}"
    runner.write_text(
        "#!/bin/sh\nset -eu\n"
        + source[start:end]
        + '\ndiscard_own_state "$1" "test state"\n'
    )
    runner.chmod(0o755)
    return subprocess.run(
        [runner, target],
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("script", (PREINST, POSTINST), ids=("preinst", "postinst"))
def test_malformed_own_state_is_discarded_without_following_symlinks(
    tmp_path: Path, script: Path
) -> None:
    # Catches maintainer scripts that exit on malformed package-owned state
    # instead of regenerating it, and a discard that follows a planted symlink.
    malformed = tmp_path / "state"
    malformed.write_text("not a valid record\n")
    assert _run_discard_own_state(tmp_path, script, malformed).returncode == 0
    assert not malformed.exists()

    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    link = tmp_path / "link"
    link.symlink_to(victim)
    assert _run_discard_own_state(tmp_path, script, link).returncode == 0
    assert not link.is_symlink()
    assert victim.read_text() == "keep\n"

    directory = tmp_path / "directory"
    directory.mkdir()
    assert _run_discard_own_state(tmp_path, script, directory).returncode != 0
    assert directory.is_dir()

    absent = tmp_path / "absent"
    assert _run_discard_own_state(tmp_path, script, absent).returncode == 0


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


@pytest.mark.parametrize("action", ("remove", "purge"))
def test_postrm_forgets_unit_enablement_on_purge_only(
    tmp_path: Path, action: str
) -> None:
    """Catches enablement state surviving a purge, or a plain remove purging it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    helper = bin_dir / "deb-systemd-helper"
    helper.write_text(f'#!/bin/sh\necho "$@" >> {shlex.quote(str(log))}\n')
    helper.chmod(0o755)
    environment = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["/bin/sh", str(POSTRM), action],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    if action == "remove":
        assert calls == []
    else:
        enabled = re.findall(
            r"deb-systemd-helper enable (vonk-forge-[\w.-]+)", POSTINST.read_text()
        )
        assert enabled
        assert sorted(calls) == sorted(f"purge {unit}" for unit in enabled)


def test_prerm_disables_every_unit_the_package_enables_on_removal() -> None:
    prerm = PRERM.read_text()
    postinst = POSTINST.read_text()
    enabled = set(
        re.findall(r"deb-systemd-helper enable (vonk-forge-[\w.-]+)", postinst)
    )
    assert enabled
    for unit in enabled:
        assert f"deb-systemd-helper disable {unit}" in prerm


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

    assert '&& { [ -n "${2:-}" ] || [ "$pending_present" -eq 1 ]; };' in postinst
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


def test_package_lifecycle_accepts_only_current_pending_and_has_no_bridge() -> None:
    preinst = PREINST.read_text()
    postinst = POSTINST.read_text()
    prerm = PRERM.read_text()

    for lifecycle in (preinst, postinst, prerm):
        assert "state=pre-unpack" not in lifecycle
    assert '[ "$(/usr/bin/wc -l < "$pending")" -eq 3 ]' in preinst
    assert '[ "$(/usr/bin/wc -l < "$helper_pending")" -eq 3 ]' in postinst
    assert '[ "$(/usr/bin/wc -l < "$pending")" -eq 3 ]' in prerm
    assert "bridge_dropin" not in postinst
    assert "upgrade-bridge" not in postinst
    activation = postinst[
        postinst.index("activate_permanent_helper_unit()") : postinst.index(
            "schedule_permanent_helper_restart()"
        )
    ]
    assert "ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent" in (
        activation
    )
    assert "/usr/bin/systemctl --system daemon-reload" in activation


def test_controller_upgrade_commits_recovery_before_pending_gate() -> None:
    preinst = PREINST.read_text()
    postinst = POSTINST.read_text()
    cache_commit = preinst.index('/usr/bin/mv -- "$cached_new" "$cached_package"')
    runner_commit = preinst.index('atomic_install "$0" "$runner" 555')
    unit_commit = preinst.index("stage_recovery_unit ||")
    capsule_commit = preinst.index("stage_recovery_capsule ||")
    agent_gate_commit = preinst.index("stage_agent_gate ||")
    blocker_commit = preinst.index('atomic_text "$agent_blocker" 600')
    intent_commit = preinst.index('atomic_text "$intent" 600')
    service_start = preinst.index(
        '--no-block start "$capsule_unit_name"', intent_commit
    )
    pending_commit = preinst.index('atomic_text "$pending" 600', service_start)

    assert (
        cache_commit < runner_commit < unit_commit < capsule_commit < agent_gate_commit
    )
    assert agent_gate_commit < intent_commit < blocker_commit
    assert intent_commit < service_start < pending_commit
    assert "package_sha256=$package_digest" in preinst
    assert "recovery_nonce=$recovery_nonce" in preinst
    assert '/usr/bin/sync -f "$state_dir"' in preinst
    assert "durable_recovery=1" in postinst
    assert '[ "$durable_recovery" -ne 1 ]' in postinst


def test_recovery_binds_exact_root_custody_dpkg_invocation_and_candidate() -> None:
    preinst = PREINST.read_text()

    assert '"$(/usr/bin/readlink -f "/proc/$PPID/exe")" = /usr/bin/dpkg' in preinst
    assert "= --install" in preinst
    assert "= --force-confold" in preinst
    assert "helper_legacy_incoming" not in preinst
    assert "custody_root=/run/vonk-forge-package-candidates" in preinst
    assert '[ "${#invocation}" -eq 32 ]' in preinst
    assert "= 0:0:600:1 ]" in preinst
    assert 'safe_root_directory "$invocation_dir" 700' in preinst
    assert "candidate_before=" in preinst and "candidate_after=" in preinst
    assert "helper_namespace_has_package_paths" in preinst
    assert "inherited helper sandbox lacks package lifecycle paths" in preinst
    assert "helper sandbox keyring directory is unsafe" in preinst
    assert "helper sandbox package document directory is unsafe" in preinst
    assert "helper sandbox keyring directory is not writable" in preinst
    assert "helper sandbox package document directory is not writable" in preinst
    assert "helper sandbox write probes have unsafe metadata" in preinst
    assert "recovery capsule will retry outside it" in preinst
    namespace_probe = preinst.index("if ! helper_namespace_has_package_paths")
    namespace_handoff = preinst[namespace_probe:]
    assert "exit 1" not in namespace_handoff.split("fi\nfi\n\nexit 0", 1)[0]
    verifier = VERIFY.read_text()
    assert "inherited helper sandbox lacks package lifecycle paths" in verifier
    assert "durable recovery armed outside the inherited helper sandbox" in verifier
    assert "preinst helper sandbox handoff is incomplete" in verifier
    recovery_arm = preinst.index("if ! arm_controller_recovery; then")
    assert recovery_arm < namespace_probe
    assert "installed signed helper copies the agent-owned download" in preinst
    assert "root-only per-invocation custody directory" in preinst
    assert "controller recovery preflight failed" in preinst
    assert "package-upgrade.preflight.status" in preinst
    assert "continuing package install" in preinst
    assert "continue with package finisher" in preinst


def test_recovery_does_not_probe_legacy_helper_socket_metadata() -> None:
    preinst = PREINST.read_text()
    start = preinst.index('    ensure_root_directory "$socket_dropin_dir" 755')
    end = preinst.index("\n    boot_id=", start)
    handoff = preinst[start:end]
    assert "systemctl --system is-enabled" not in handoff
    assert "systemctl --system is-active" not in handoff
    assert "namespace restrictions" in handoff


def test_durable_recovery_fallback_is_exact_package_scoped_and_nonce_bound() -> None:
    preinst = PREINST.read_text()
    prerm = PRERM.read_text()

    fallback = preinst[preinst.index("reinstall_exact_package()") :]
    assert "safe_cached_package" in fallback
    assert '[ "$intent_version" = "$target_version" ]' in fallback
    assert '[ "$intent_architecture" = "$target_architecture" ]' in fallback
    assert (
        'dpkg --compare-versions "$installed_version" gt "$intent_version"' in fallback
    )
    assert (
        'VONK_FORGE_UPGRADE_RECOVERY_NONCE=$recovery_nonce \\\n        /usr/bin/dpkg --remove --force-remove-reinstreq "$package_name"'
        in fallback
    )
    assert (
        'VONK_FORGE_UPGRADE_RECOVERY_NONCE=$recovery_nonce \\\n        /usr/bin/dpkg --install --force-confold "$cached_package"'
        in fallback
    )
    assert "/usr/bin/apt" not in fallback
    assert "--configure -a" not in fallback

    assert "recovery_remove_binding()" in prerm
    assert 'grep -Fxq "0::/system.slice/$recovery_capsule_unit"' in prerm
    assert '[ "$(/usr/bin/wc -l < "$upgrade_intent")" -eq 17 ]' in prerm
    assert "recovery_nonce=$VONK_FORGE_UPGRADE_RECOVERY_NONCE" in prerm
    assert "--force-remove-reinstreq" in prerm
    assert "Preserve its gates, service enablement, node config" in prerm


def test_durable_recovery_capsule_is_single_owner_boot_gated_and_intent_retired_first() -> (
    None
):
    preinst = PREINST.read_text()
    capsule_unit = (
        ROOT / "packaging/systemd/vonk-forge-package-upgrade-recover-capsule.service"
    ).read_text()
    capsule_gate = (
        ROOT / "packaging/systemd/vonk-forge-agent.service.d/"
        "10-package-upgrade-capsule.conf"
    ).read_text()
    suppression = (
        ROOT / "packaging/systemd/vonk-forge-package-upgrade-recover.service.d/"
        "10-capsule-owner.conf"
    ).read_text()

    assert "schema_version=2" in preinst
    for binding in (
        "capsule_unit_sha256=",
        "capsule_gate_sha256=",
        "capsule_suppression_sha256=",
    ):
        assert binding in preinst
    assert "safe_recovery_capsule || abandon_intent recovery-capsule" in preinst
    assert "safe_capsule_enablement" in preinst
    assert (
        'if [ -e "$embedded_destination" ] || [ -L "$embedded_destination" ]' in preinst
    )
    intent_retire = preinst.index('/usr/bin/rm -- "$intent"')
    capsule_retire = preinst.index("if ! retire_recovery_capsule; then", intent_retire)
    assert intent_retire < capsule_retire
    assert (
        "ExecStart=/var/lib/vonk-forge/package-upgrade/recovery-capsule/runner"
        in capsule_unit
    )
    assert "WantedBy=multi-user.target" in capsule_unit
    assert "ExecCondition=+/var/lib/vonk-forge/package-upgrade/" in capsule_gate
    assert (
        "ConditionPathExists=!/var/lib/vonk-forge/package-upgrade/intent" in suppression
    )
    assert "capsule_admin_root=/etc/systemd/system" in preinst
    assert "capsule_wants_dir=/lib/systemd/system/multi-user.target.wants" in preinst
    assert '/usr/bin/ln -s "../$capsule_unit_name" "$capsule_enablement"' in preinst
    assert "capsule_runtime_root=/run/systemd/system" in preinst
    assert "capsule_shadow_paths_absent || return 1" in preinst


@pytest.mark.parametrize(
    ("relative", "kind"),
    (
        ("vonk-forge-package-upgrade-recover-capsule.service", "file"),
        (
            "multi-user.target.wants/vonk-forge-package-upgrade-recover-capsule.service",
            "symlink",
        ),
    ),
)
def test_durable_recovery_capsule_rejects_admin_unit_and_enablement_collisions(
    tmp_path: Path, relative: str, kind: str
) -> None:
    preinst = PREINST.read_text()
    start = preinst.index("capsule_shadow_paths_absent() {")
    end = preinst.index("\n}\n", start) + 3
    function = preinst[start:end]
    admin_root = tmp_path / "etc-systemd"
    runtime_root = tmp_path / "run-systemd"
    admin_root.mkdir()
    runtime_root.mkdir()
    collision = admin_root / relative
    collision.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        collision.write_text("collision\n", encoding="utf-8")
    else:
        collision.symlink_to("../different.service")
    script = "\n".join(
        (
            f"capsule_admin_root={shlex.quote(str(admin_root))}",
            f"capsule_runtime_root={shlex.quote(str(runtime_root))}",
            "capsule_unit_name=vonk-forge-package-upgrade-recover-capsule.service",
            "unit_name=vonk-forge-package-upgrade-recover.service",
            function,
            "capsule_shadow_paths_absent",
        )
    )

    result = subprocess.run(
        ["/bin/sh"], input=script, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0


def test_durable_recovery_capsule_enablement_is_exact_and_vendor_owned(
    tmp_path: Path,
) -> None:
    preinst = PREINST.read_text()
    start = preinst.index("safe_capsule_enablement() {")
    end = preinst.index("\n}\n", start) + 3
    function = preinst[start:end]
    unit = tmp_path / "lib/systemd/system/recovery.service"
    enablement = (
        tmp_path / "lib/systemd/system/multi-user.target.wants/recovery.service"
    )
    unit.parent.mkdir(parents=True)
    enablement.parent.mkdir(parents=True)
    unit.write_text("unit\n", encoding="utf-8")
    enablement.symlink_to("../recovery.service")
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f"{function}\nsafe_capsule_enablement",
        ],
        check=False,
        env={
            **os.environ,
            "capsule_enablement": str(enablement),
            "capsule_unit_name": "recovery.service",
        },
    )
    assert result.returncode == 0
    enablement.unlink()
    enablement.symlink_to("/tmp/foreign-recovery.service")
    result = subprocess.run(
        ["/bin/sh", "-c", f"{function}\nsafe_capsule_enablement"],
        check=False,
        env={
            **os.environ,
            "capsule_enablement": str(enablement),
            "capsule_unit_name": "recovery.service",
        },
    )
    assert result.returncode != 0


def test_recovery_status_is_bounded_stage_only_and_exercised_natively() -> None:
    preinst = PREINST.read_text()
    lifecycle = RECOVERY_LIFECYCLE.read_text()

    assert "status_receipt=$state_parent/package-upgrade.status" in preinst
    assert "| /usr/bin/cut -c 1-64" in preinst
    for stage in (
        "package-state",
        "package-configure",
        "package-install",
        "package-remove",
        "package-reinstall",
        "package-proof",
        "helper-proof",
        "agent-proof",
        "complete",
    ):
        assert stage in preinst
    assert "outcome=succeeded" in lifecycle
    assert "stage=complete" in lifecycle
    assert "package-upgrade.status" in lifecycle


def test_root_custody_lifecycle_executes_the_exact_real_dpkg_contract() -> None:
    lifecycle = RECOVERY_LIFECYCLE.read_text()

    assert "CANDIDATE_CUSTODY" not in lifecycle
    assert "custody_root=/run/vonk-forge-package-candidates" in lifecycle
    assert "custody_invocation=0123456789abcdef0123456789abcdef" in lifecycle
    assert (
        "candidate=$custody_root/$custody_invocation/$package_digest.deb" in lifecycle
    )
    assert "RuntimeDirectory=vonk-forge-package-candidates" in lifecycle
    assert "RuntimeDirectoryMode=0700" in lifecycle
    assert "RuntimeDirectoryPreserve=restart" in lifecycle
    assert 'test "$(stat -c %u:%g:%a "$custody_root")" = 0:0:700' in lifecycle
    assert 'test "$(stat -c %u:%g:%a:%h "$candidate")" = 0:0:600:1' in lifecycle
    assert "mapfile -d '' -t dpkg_argv < \"/proc/$dpkg_pid/cmdline\"" in lifecycle
    assert 'test "${dpkg_argv[0]}" = /usr/bin/dpkg' in lifecycle
    assert 'test "${dpkg_argv[1]}" = --install' in lifecycle
    assert 'test "${dpkg_argv[2]}" = --force-confold' in lifecycle
    assert 'test "${dpkg_argv[3]}" = "$candidate"' in lifecycle
    assert (
        "ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent"
        in lifecycle.splitlines()
    )
    assert (
        "upgrade_invocations=/var/lib/vonk-forge/upgrade-invocations.$(basename "
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
        '"$package_recovery_gate"',
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
        (
            "BindReadOnlyPaths=-/run/docker.sock "
            "-/run/vonk-forge-agent/runtime-requests "
            "-/var/lib/vonk-forge-agent/image-imports"
        ),
        (
            "ReadWritePaths=-/var/lib/vonk-forge-agent/models "
            "-/var/lib/vonk-forge-agent/runs "
            "-/var/lib/vonk-forge-agent/run-metadata"
        ),
        "TimeoutStartSec=30s",
        "TimeoutStopSec=15s",
        "KillMode=mixed",
    ):
        assert historical_line in lifecycle.splitlines()

    assert "crash_pending_kind=stale" in lifecycle
    assert "crash_pending_kind=normalized" in lifecycle
    freeze = lifecycle.index('systemctl --system freeze "$helper_unit"')
    trigger = lifecycle.index("if [[ -f /var/lib/vonk-forge/package-upgrade/intent ]]")
    assert trigger < freeze
    assert "intent_sync_quiescent" not in lifecycle[trigger:freeze]
    assert '"$test_root/crash-point-pending"' in lifecycle
    assert 'cmp -s "$test_root/normalized-pending"' in lifecycle
    assert "assert_interrupted_baseline_state()" in lifecycle
    assert "assert_interrupted_baseline_state" in lifecycle[freeze:]

    assert '"iU |$baseline_version"|"iHR|$baseline_version"' in lifecycle
    assert "unexpected interrupted package state" in lifecycle
    assert "durable lower-interrupted" in lifecycle
    assert "trap thaw_helper EXIT" in lifecycle
    crash_snapshot = lifecycle.index("crash_intent_digest=")
    frozen_kill = lifecycle.index(
        "systemctl --system kill --kill-whom=all --signal=SIGSTOP"
    )
    thaw_before_kill = lifecycle.index(
        'systemctl --system thaw "$helper_unit"', frozen_kill
    )
    final_kill = lifecycle.index(
        "systemctl --system kill --kill-whom=all --signal=SIGKILL",
        thaw_before_kill,
    )
    assert crash_snapshot < frozen_kill < thaw_before_kill < final_kill
    assert lifecycle.index("FreezerState", thaw_before_kill) < final_kill
    crash_window = lifecycle[crash_snapshot:final_kill]
    assert "helper_control_group" in crash_window
    assert "all_helper_pids_quiescent" in crash_window
    assert "T|t|Z" in crash_window
    assert "D)" in crash_window
    assert 'helper_pid_exe" != /usr/bin/sync' in crash_window
    assert '"${#helper_pid_argv[@]}" -ne 3' in crash_window
    assert '"${helper_pid_argv[0]}" != /usr/bin/sync' in crash_window
    assert '"${helper_pid_argv[1]}" != -f' in crash_window
    for safe_sync_target in (
        "/var/lib/vonk-forge/package-upgrade",
        "/var/lib/vonk-forge",
        "[0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz]{6}",
    ):
        assert safe_sync_target in crash_window
    assert ".vonk-upgrade.??????" not in crash_window
    assert "%F" not in crash_window
    assert "'0:0:700'" in crash_window
    assert "'0:0:755'" in crash_window
    assert "'0:0:600:1'" in crash_window
    assert '"$safe_sync_target" -ne 1' in crash_window
    for exact_preinst_gate in (
        'helper_pid_exe" == /usr/bin/dash',
        '"${#helper_pid_argv[@]}" -eq 5',
        '"${helper_pid_argv[0]}" == /bin/sh',
        '"${helper_pid_argv[1]}" == /var/lib/dpkg/tmp.ci/preinst',
        '"${helper_pid_argv[2]}" == upgrade',
        '"${helper_pid_argv[3]}" == "$baseline_version"',
        '"${helper_pid_argv[4]}" == "$version"',
        "== '0:0:755:1'",
    ):
        assert exact_preinst_gate in crash_window
    assert '"$safe_d_state" -ne 1' in crash_window
    assert 'case "$dpkg_pid_state" in T|t)' in crash_window
    assert "captured helper pid=%s state=%s exe=%q argv=" in crash_window
    assert 'done < "/proc/$helper_pid/status" 2>/dev/null || continue' in crash_window
    assert 'done < "/proc/$dpkg_pid/status" 2>/dev/null || exit 1' in crash_window
    assert "crash_intent_digest" in crash_window
    post_kill = lifecycle[final_kill:]
    assert "helper_main_pid_after" in post_kill
    state_check = post_kill.split('test "$helper_main_pid_after" = 0', 1)[1].split(
        'test "$helper_freezer_state" = running', 1
    )[0]
    for active, sub, accepted in (
        ("failed", "failed", True),
        ("activating", "auto-restart", True),
        ("activating", "start", False),
        ("active", "running", False),
        ("inactive", "dead", False),
    ):
        result = subprocess.run(
            ["bash", "-c", state_check],
            env={**os.environ, "helper_active_state": active, "helper_sub_state": sub},
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert (result.returncode == 0) == accepted
    assert 'test "$helper_freezer_state" = running' in post_kill
    assert "--property=Result" in post_kill
    dpkg_only_start = lifecycle.index("        dpkg-only|post-remove)", crash_snapshot)
    dpkg_only_end = lifecycle.index("\n          ;;", dpkg_only_start)
    dpkg_only = lifecycle[dpkg_only_start:dpkg_only_end]
    crash_pending_check = dpkg_only.index('cmp -s "$test_root/crash-point-pending"')
    assert '"$test_root/normalized-pending"' not in dpkg_only
    frozen_state_check = dpkg_only.index('"$helper_unit")" = frozen')
    recovery_start = dpkg_only.index(
        'systemctl --system --no-block start "$recovery_unit"'
    )
    kill_dpkg = dpkg_only.index('kill -KILL "$dpkg_pid"')
    intent_digest_check = dpkg_only.index('= "$crash_intent_digest"')
    baseline_state_check = dpkg_only.index("assert_interrupted_baseline_state")
    thaw_loop_start = dpkg_only.index("for _ in {1..100}; do", kill_dpkg)
    thaw = dpkg_only.index(
        'systemctl --system thaw "$helper_unit" \\\n'
        "              >/dev/null 2>&1 || true",
        thaw_loop_start,
    )
    freezer_read = dpkg_only.index("--property=FreezerState", thaw)
    running_break = dpkg_only.index(
        '[[ "$helper_freezer_state" == running ]]', freezer_read
    )
    retry_sleep = dpkg_only.index("sleep 0.05", running_break)
    loop_end = dpkg_only.index("          done", retry_sleep)
    terminal_state = dpkg_only.index('test "$helper_freezer_state" = running', loop_end)
    assert (
        frozen_state_check
        < crash_pending_check
        < intent_digest_check
        < baseline_state_check
        < recovery_start
        < kill_dpkg
        < thaw_loop_start
        < thaw
        < freezer_read
        < running_break
        < retry_sleep
        < loop_end
        < terminal_state
    )
    assert '"$helper_unit")" = frozen' in dpkg_only
    assert (
        '"$helper_unit")" = frozen'
        not in dpkg_only.partition('"$helper_unit")" = frozen')[2]
    )
    assert "recovery_active_state" not in dpkg_only
    assert "recovery_main_pid" not in dpkg_only
    assert "--property=ActiveState" not in dpkg_only
    assert "--property=MainPID" not in dpkg_only
    assert "sleep 1" not in dpkg_only
    boot_comment = lifecycle.index("A real boot does not preserve the test-only cgroup")
    watcher_wait = lifecycle.index('wait "$crash_watcher"')
    crash_observed_assert = lifecycle.index('test -f "$crash_observed"', watcher_wait)
    full_cgroup_branch = lifecycle.index(
        'if [[ "$crash_mode" == full-cgroup ]]; then', dpkg_only_end
    )
    intent_must_exist = lifecycle.index(
        "test -f /var/lib/vonk-forge/package-upgrade/intent", full_cgroup_branch
    )
    post_watcher = lifecycle[watcher_wait:boot_comment]
    assert "test -f /var/lib/vonk-forge/package-upgrade/intent" in post_watcher
    assert (
        "test -f /var/lib/vonk-forge/package-upgrade/intent"
        not in post_watcher.partition(
            "test -f /var/lib/vonk-forge/package-upgrade/intent"
        )[2]
    )
    assert (
        watcher_wait
        < crash_observed_assert
        < full_cgroup_branch
        < intent_must_exist
        < boot_comment
    )
    boot_simulation = lifecycle.index(
        'systemctl --system thaw "$helper_unit"', boot_comment
    )
    assert lifecycle.index("FreezerState", boot_simulation) > boot_simulation
    reset_failed = lifecycle.index("systemctl --system reset-failed", boot_simulation)
    assert lifecycle.index('"$helper_unit"', reset_failed) > reset_failed
    assert lifecycle.index('"$socket_unit"', reset_failed) > reset_failed
    assert lifecycle.index("ActiveState", reset_failed) > reset_failed
    assert lifecycle.index("Result", reset_failed) > reset_failed
    full_cgroup_end = lifecycle.index(
        '\nif [[ "$crash_mode" == post-remove ]]; then', full_cgroup_branch
    )
    full_cgroup_boot = lifecycle[full_cgroup_branch:full_cgroup_end]
    full_cgroup_wants = full_cgroup_boot.index(
        "systemctl --system show --property=Wants --value"
    )
    full_cgroup_capsule_start = full_cgroup_boot.index(
        'systemctl --system start "$recovery_unit"'
    )
    assert full_cgroup_wants < full_cgroup_capsule_start
    assert "capsule owns a durable schema2 intent" in full_cgroup_boot
    convergence_marker = lifecycle.index(
        "printf 'durable recovery crash-point pending gate:", reset_failed
    )
    convergence = lifecycle.index("for _ in {1..1200}", convergence_marker)
    receipt_check = lifecycle.index("helper-upgrade.receipt", convergence)
    load_state_check = lifecycle.index("recovery_load_state=", convergence)
    recovery_active_check = lifecycle.index("recovery_active_state=", convergence)
    recovery_sub_check = lifecycle.index("recovery_sub_state=", convergence)
    recovery_pid_check = lifecycle.index("recovery_main_pid=", convergence)
    active_state_check = lifecycle.index("package_recovery_active_state=", convergence)
    sub_state_check = lifecycle.index("package_recovery_sub_state=", convergence)
    blocked_cleanup_check = lifecycle.index(
        "package-upgrade/agent-blocked", convergence
    )
    cached_package_cleanup_check = lifecycle.index(
        "package-upgrade/$package_digest.deb", convergence
    )
    convergence_break = lifecycle.index("break", convergence)
    assert (
        load_state_check
        < recovery_active_check
        < recovery_sub_check
        < recovery_pid_check
        < active_state_check
        < sub_state_check
        < receipt_check
        < blocked_cleanup_check
        < cached_package_cleanup_check
        < convergence_break
    )
    post_remove = lifecycle[
        lifecycle.index('if [[ "$crash_mode" == post-remove ]]') : convergence_marker
    ]
    for durable_proof in (
        "test-post-remove-preinst-entered",
        "stage=package-reinstall",
        "--kill-whom=all --signal=SIGKILL",
        "test ! -e /usr/lib/vonk-forge/vonk-forge-package-upgrade-recover",
        "test ! -e /lib/systemd/system/vonk-forge-package-upgrade-recover.service",
        'test -f "$recovery_unit_path"',
        'test -L "$recovery_enablement"',
        "agent unexpectedly started before capsule recovery",
        "systemctl --system show --property=Wants --value",
        'systemctl --system start "$recovery_unit"',
    ):
        assert durable_proof in post_remove
    wants_proof = post_remove.index("systemctl --system show --property=Wants --value")
    simulated_boot_start = post_remove.index(
        'systemctl --system start "$recovery_unit"'
    )
    assert wants_proof < simulated_boot_start
    assert "systemctl --system restart multi-user.target" not in post_remove
    assert (
        '"$recovery_load_state" == "$expected_recovery_load_state"'
        in lifecycle[convergence:convergence_break]
    )
    assert (
        '"$package_recovery_active_state" == inactive'
        in lifecycle[convergence:convergence_break]
    )
    assert (
        '"$package_recovery_sub_state" == dead'
        in lifecycle[convergence:convergence_break]
    )
    assert lifecycle.index(
        'test "$recovery_load_state" = "$expected_recovery_load_state"',
        convergence_break,
    )
    assert lifecycle.index(
        'test "$package_recovery_active_state" = inactive', convergence_break
    )
    assert lifecycle.index(
        'test "$package_recovery_sub_state" = dead', convergence_break
    )
    assert "trap 'exit 129' HUP" in lifecycle
    assert "trap 'exit 130' INT" in lifecycle
    assert "trap 'exit 143' TERM" in lifecycle
    assert "dump_failure_diagnostics" in lifecycle
    assert "failed assertion: line=%s status=%s" in lifecycle
    assert "recovery_failed_line=$LINENO" in lifecycle
    assert "journalctl --system --no-pager -n 200" in lifecycle
    assert "firewall_fixture=/run/systemd/system/$firewall_unit" in lifecycle
    assert "Vonk Forge package recovery firewall fixture" in lifecycle
    assert '"$firewall_unit"' in lifecycle
    assert (
        "install -d -o vonk-agent -g vonk-agent -m 0700 "
        "/var/lib/vonk-forge-agent" in lifecycle
    )
    collision_check = lifecycle.index(
        "agent upgrade recovery fixture would collide with host state"
    )
    assert "/var/lib/vonk-forge-agent" in lifecycle[:collision_check]
    cleanup = lifecycle[
        lifecycle.index("cleanup() {") : lifecycle.index("cleanup_test_root() {")
    ]
    recovery_state_cleanup = cleanup.index(
        "rm -rf -- /var/lib/vonk-forge/package-upgrade"
    )
    package_purge = cleanup.index(
        "dpkg --purge --force-remove-reinstreq", recovery_state_cleanup
    )
    home_cleanup = cleanup.index("rm -rf -- /var/lib/vonk-forge-agent", package_purge)
    assert recovery_state_cleanup < package_purge < home_cleanup
    assert "dpkg-query --show vonk-forge-agent" in cleanup
    assert 'trap - EXIT\n  exit "$cleanup_status"' in cleanup
    assert 'return "$cleanup_status"' not in cleanup
    assert "recovery_nonce)=.*/\\1=<redacted>" in lifecycle


def test_recovery_is_static_offline_named_only_and_compare_deletes() -> None:
    preinst = PREINST.read_text()
    socket = (ROOT / "packaging/systemd/vonk-forge-package-helper.socket").read_text()
    recovery = (
        ROOT / "packaging/systemd/vonk-forge-package-upgrade-recover.service"
    ).read_text()
    agent_gate = (
        ROOT / "packaging/systemd/vonk-forge-agent.service.d/"
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
    assert "state=pre-unpack" not in preinst
    pending_guard = preinst[preinst.index("safe_known_pending()") : normalize]
    assert '[ "$(/usr/bin/wc -l < "$pending")" -eq 3 ]' in pending_guard
    blocker_retire = preinst.index(
        'discard_own_state "$agent_blocker" || unsafe_state',
        preinst.index("recover_upgrade() {"),
    )
    agent_restart = preinst.index('restart "$agent_unit"', blocker_retire)
    intent_retire = preinst.index('/usr/bin/rm -- "$intent"', agent_restart)
    assert blocker_retire < agent_restart < intent_retire
    assert "IPAddressDeny=any" in recovery
    assert 'dpkg --force-confold --configure "$package_name"' in preinst
    assert 'dpkg --install --force-confold "$cached_package"' in preinst
    assert "dpkg --configure -a" not in preinst
    assert "apt-get" not in preinst and "curl" not in preinst
    assert "intent_snapshot" in preinst
    assert "intent changed before gate retirement" in preinst
    assert "intent changed before retirement" in preinst
    assert '"/proc/$service_pid/exe"' in preinst
    assert '"/proc/$helper_pid/cgroup"' in preinst
    # Packaged executables are deliberately root-owned and non-writable (0555).
    # The bounded ancestry fallback must recognize that deployed mode while
    # retaining compatibility with a locally repaired 0755 helper.
    assert "0:555|0:755) return 0" in preinst
    assert "legacy_bridge" not in preinst
    assert "20-package-upgrade-bridge.conf" not in preinst


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


def test_package_helper_detection_accepts_private_cgroup_custody_invocation() -> None:
    preinst = PREINST.read_text()

    # A helper launched in a private cgroup namespace may not expose its
    # service name to the maintainer script.  The fallback must therefore bind
    # recovery only to the exact root-owned custody dpkg command emitted by the
    # signed helper, never to a generic administrator dpkg invocation.
    assert "/run/vonk-forge-package-candidates/*/*.deb" in preinst
    assert "--force-confold" in preinst
    assert "0:0:600:1" in preinst
    assert "Ordinary package-manager" in preinst


def test_package_helper_restart_waits_for_verified_exec_identity() -> None:
    helper_unit = (
        ROOT / "packaging/systemd/vonk-forge-package-helper.service"
    ).read_text()
    postinst = POSTINST.read_text()
    preinst = PREINST.read_text()

    assert "Type=exec" in helper_unit.splitlines()
    assert "Type=simple" not in helper_unit.splitlines()
    assert "ExecStart=/usr/lib/vonk-forge/vonk-agent-helper" in helper_unit.splitlines()
    assert (
        "/usr/bin/systemctl --system restart \\\n"
        "                vonk-forge-package-helper.service"
    ) in postinst
    assert 'restart "$helper_unit"' in preinst
    assert '"/proc/$new_helper_pid/exe"' in postinst
    assert '"/proc/$service_pid/exe"' in preinst


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
    receipt_validation = postinst.index('grep -Fxq "schema_version=2"')
    confirmed_digest = postinst.index("confirmed_helper_digest=", receipt_validation)
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
    assert helper_restart < running_digest < installed_digest < receipt_validation
    assert receipt_validation < confirmed_digest < pending_retire
    assert pending_retire < pending_retire_sync < agent_restart < schedule_call
    assert "helper-upgrade.pending" in postinst
    assert "helper-upgrade.receipt" in postinst
    assert '"schema_version=2"' in postinst
    assert '"schema_version=1"' not in postinst
    assert "receipt_new=" not in postinst
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
    schedule = postinst.index('schedule_permanent_helper_restart "$PPID" "$dpkg_start"')
    assert pending_probe < finisher_decision < pending_write < schedule
    assert (
        "interrupted helper activation requires a live systemd configure retry"
        in postinst
    )
    # Malformed pending state is discarded and rewritten, not a dpkg failure;
    # only an unsafe root-owned parent directory still refuses.
    pending_discard = postinst.index(
        "discard_own_state \"$helper_pending\" 'pending helper upgrade state'"
    )
    assert pending_probe < pending_discard < pending_write

    stop_finisher = prerm.index("vonk-forge-package-helper-upgrade-finish.service")
    refuse_durable = prerm.index("removal supersedes durable upgrade recovery")
    stop_agent = prerm.index("deb-systemd-invoke stop vonk-forge-agent.service")
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


def test_agent_uses_package_managed_private_temp_directory() -> None:
    postinst = POSTINST.read_text()
    unit = (ROOT / "packaging/systemd/vonk-forge-agent.service").read_text()

    assert "install -d -o vonk-agent -g vonk-agent -m 0700 \\" in postinst
    assert "/var/lib/vonk-forge-agent/tmp" in postinst
    assert "Environment=TMPDIR=/var/lib/vonk-forge-agent/tmp" in unit


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


def test_ordinary_control_verification_remains_unchanged(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir()
    for name in VERIFY_MODULE.REQUIRED_CONTROL:
        source = ROOT / f"packaging/debian/{name}"
        path = control / name
        path.write_bytes(source.read_bytes() if source.is_file() else b"")
        path.chmod(
            0o755 if name in {"preinst", "postinst", "postrm", "prerm"} else 0o644
        )

    VERIFY_MODULE._verify_control(control)


def _unsealed_build(output: Path, binaries: Path) -> subprocess.CompletedProcess[str]:
    (binaries / "oras.LICENSE").write_text("ORAS test license\n")
    return subprocess.run(
        [
            BUILD,
            "--version",
            "0.1.1",
            "--architecture",
            "linux-arm64",
            "--build-digest",
            BUILD_DIGEST,
            "--unsealed",
            "--binaries-dir",
            binaries,
            "--source-date-epoch",
            "1786060800",
            "--output-dir",
            output,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _seal(unsealed: Path, key: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            ROOT / "scripts/seal-agent-deb",
            "--unsealed",
            unsealed,
            "--release-private-key",
            key,
            "--output-dir",
            output,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _members(package: Path) -> set[str]:
    listing = subprocess.run(
        ["/usr/bin/dpkg-deb", "--fsys-tarfile", package],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(listing)) as archive:
        return {member.name.removeprefix("./") for member in archive.getmembers()}


def test_sealing_the_unsealed_build_is_exactly_the_keyed_build(tmp_path: Path) -> None:
    """Catches a seal that changes anything but the trust material: publication
    must be able to seal exactly the package an earlier run built and accepted."""
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    keyed = tmp_path / "keyed"
    unsealed_dir = tmp_path / "unsealed"
    sealed = tmp_path / "sealed"
    assert _build(keyed, binaries, key).returncode == 0
    result = _unsealed_build(unsealed_dir, binaries)
    assert result.returncode == 0, result.stderr
    unsealed = unsealed_dir / "vonk-forge-agent_0.1.1_arm64.unsealed.deb"
    package_name = "vonk-forge-agent_0.1.1_arm64.deb"
    assert not (unsealed_dir / package_name).exists()
    trust = {
        "usr/lib/vonk-forge/release/artifact.sig",
        "usr/share/keyrings/vonk-forge-release.pub",
    }
    assert not trust & _members(unsealed)
    assert trust <= _members(keyed / package_name)

    sealing = _seal(unsealed, key, sealed)

    assert sealing.returncode == 0, sealing.stderr
    for name in (package_name, f"{package_name}.sha256", f"{package_name}.host.sig"):
        assert (sealed / name).read_bytes() == (keyed / name).read_bytes(), name


def test_main_recovers_exactly_the_packages_a_pull_request_accepted(
    tmp_path: Path,
) -> None:
    """Catches main sealing anything but the unsealed packages the pull
    request built: the recovered packages equal unsealed builds byte for byte,
    and versions outside this source's development scheme are refused."""
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    baseline_binaries = tmp_path / "baseline-binaries"
    baseline_binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(baseline_binaries / name, name.encode())
    agent = baseline_binaries / "vonk-agent"
    agent.chmod(0o755)
    agent.write_bytes(
        agent.read_bytes().replace(
            b"VONK_AGENT_SEMANTIC_VERSION=0.1.1", b"VONK_AGENT_SEMANTIC_VERSION=0.1.0"
        )
    )
    agent.chmod(0o555)
    key = tmp_path / "pull-request.pem"
    _release_key(key)
    version = "0.1.1~dev.1792000000+gabcdefabcdef"
    baseline = "0.1.0~acceptance.1+gabcdefabcdef"
    pr = tmp_path / "pr"
    assert _build(pr / "package", binaries, key, version=version).returncode == 0
    assert (
        _build(
            pr / "baseline",
            baseline_binaries,
            key,
            version=baseline,
            acceptance_baseline=True,
        ).returncode
        == 0
    )
    result = subprocess.run(
        [ROOT / "scripts/reuse-agent-packages", pr, tmp_path / "unsealed"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"version={version}",
        f"arm64_package=vonk-forge-agent_{version}_arm64.deb",
        f"baseline_version={baseline}",
        f"arm64_baseline_package=vonk-forge-agent_{baseline}_arm64.deb",
    ]
    for name, built_version, is_baseline in (
        ("package", version, False),
        ("baseline", baseline, True),
    ):
        direct = tmp_path / f"direct-{name}"
        command = [
            BUILD,
            "--version",
            built_version,
            "--architecture",
            "linux-arm64",
            "--build-digest",
            BUILD_DIGEST,
            "--unsealed",
            "--binaries-dir",
            baseline_binaries if is_baseline else binaries,
            "--source-date-epoch",
            "1786060800",
            "--output-dir",
            direct,
        ]
        if is_baseline:
            command.append("--acceptance-baseline")
        assert subprocess.run(command, cwd=ROOT, check=False).returncode == 0
        unsealed = f"vonk-forge-agent_{built_version}_arm64.unsealed.deb"
        assert (tmp_path / "unsealed" / unsealed).read_bytes() == (
            direct / unsealed
        ).read_bytes()

    (pr / "package" / f"vonk-forge-agent_{version}_arm64.deb").rename(
        pr / "package" / "vonk-forge-agent_0.1.1_arm64.deb"
    )
    refused = subprocess.run(
        [ROOT / "scripts/reuse-agent-packages", pr, tmp_path / "refused"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0


def _unseal(sealed: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ROOT / "scripts/seal-agent-deb", "--sealed", sealed, "--output-dir", output],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_unsealing_recovers_exactly_what_any_key_sealed(tmp_path: Path) -> None:
    """Catches an unseal that is not the exact inverse of the seal: main proves
    the package it publishes is the one a pull request accepted by comparing
    the unsealed bytes of both, whichever key sealed each."""
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    assert _unsealed_build(tmp_path / "unsealed", binaries).returncode == 0
    unsealed = tmp_path / "unsealed/vonk-forge-agent_0.1.1_arm64.unsealed.deb"
    recovered = []
    for name in ("pull-request", "production"):
        key = tmp_path / f"{name}.pem"
        _release_key(key)
        assert _seal(unsealed, key, tmp_path / name).returncode == 0
        result = _unseal(
            tmp_path / name / "vonk-forge-agent_0.1.1_arm64.deb",
            tmp_path / f"{name}-recovered",
        )
        assert result.returncode == 0, result.stderr
        recovered.append(
            (
                tmp_path / f"{name}-recovered/vonk-forge-agent_0.1.1_arm64.unsealed.deb"
            ).read_bytes()
        )
    assert recovered == [unsealed.read_bytes()] * 2


def test_unsealing_refuses_unsealed_input_and_exposes_any_other_change(
    tmp_path: Path,
) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    assert _unsealed_build(tmp_path / "unsealed", binaries).returncode == 0
    unsealed = tmp_path / "unsealed/vonk-forge-agent_0.1.1_arm64.unsealed.deb"
    assert _unseal(unsealed, tmp_path / "refused").returncode != 0
    # A package built from other bytes never unseals to the accepted package.
    (binaries / "vonk-monitor").unlink()
    _aarch64_fixture(binaries / "vonk-monitor", b"another monitor")
    assert _build(tmp_path / "other", binaries, key).returncode == 0
    result = _unseal(
        tmp_path / "other/vonk-forge-agent_0.1.1_arm64.deb", tmp_path / "recovered"
    )
    assert result.returncode == 0, result.stderr
    assert (
        tmp_path / "recovered/vonk-forge-agent_0.1.1_arm64.unsealed.deb"
    ).read_bytes() != unsealed.read_bytes()


def test_an_unsealed_package_is_never_accepted_and_never_sealed_twice(
    tmp_path: Path,
) -> None:
    """Fail closed: without trust material the verifier refuses the package,
    and a package that already carries trust material cannot be re-sealed."""
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in PACKAGE_BINARIES:
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    assert _unsealed_build(tmp_path / "unsealed", binaries).returncode == 0
    unsealed = tmp_path / "unsealed/vonk-forge-agent_0.1.1_arm64.unsealed.deb"
    verified = subprocess.run(
        [VERIFY, "--json", unsealed],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode != 0
    assert _build(tmp_path / "keyed", binaries, key).returncode == 0
    resealed = tmp_path / "resealed.unsealed.deb"
    resealed.write_bytes(
        (tmp_path / "keyed/vonk-forge-agent_0.1.1_arm64.deb").read_bytes()
    )
    refused = _seal(resealed, key, tmp_path / "out")
    assert refused.returncode != 0
    assert "must not carry trust material" in refused.stderr
    assert not (tmp_path / "out/resealed.deb").exists()


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
    package_name = "vonk-forge-agent_0.1.1_arm64.deb"
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
    assert "target_version='0.1.1'" in preinst
    assert os.access(control / "preinst", os.X_OK)
    fields = subprocess.run(
        ["/usr/bin/dpkg-deb", "--field", first_deb, "Depends"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "curl" in fields
    assert "apparmor" in fields
    assert "crun" in fields
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
    recovery_runner = payload / "usr/lib/vonk-forge/vonk-forge-package-upgrade-recover"
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
    assert oras_component["version"] == "1.3.4"
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
    assert "Environment=XDG_CONFIG_HOME=/var/lib/vonk-forge-agent/.config" in unit
    assert "Environment=XDG_RUNTIME_DIR=/run/vonk-forge-agent" in unit
    assert "ProtectControlGroups=yes" in unit
    # The rootless OCI runtime must set the hostname inside each build's private UTS
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
    assert "DeviceAllow=char-nvidia-caps rw" in unit
    assert "PrivateDevices=no" in unit
    assert "DeviceAllow=/dev/char/195:0 rw" in unit
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
    assert "Type=exec" in helper_unit.splitlines()
    assert "Type=simple" not in helper_unit.splitlines()
    assert "ExecStart=/usr/lib/vonk-forge/vonk-agent-helper" in helper_unit.splitlines()
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
        "ReadWritePaths=-/sys/fs/cgroup/system.slice -/sys/fs/cgroup/docker"
        in helper_unit.splitlines()
    )
    # Only the page-cache drop and memory compaction are writable under
    # ProtectKernelTunables.
    assert "ReadWritePaths=-/proc/sys/vm/drop_caches" in helper_unit.splitlines()
    assert "ReadWritePaths=-/proc/sys/vm/compact_memory" in helper_unit.splitlines()
    assert "ProtectKernelTunables=yes" in helper_unit.splitlines()
    # Reject broader capability sets, including a second additive directive
    # that leaves the expected line intact (the former substring check missed it).
    helper_path = payload / "lib/systemd/system/vonk-forge-package-helper.service"
    for broadened in (
        helper_unit.replace(
            "CAP_SYS_ADMIN CAP_CHOWN", "CAP_SYS_PTRACE CAP_SYS_ADMIN CAP_CHOWN"
        ),
        helper_unit.replace(
            "AmbientCapabilities=",
            "CapabilityBoundingSet=CAP_SYS_PTRACE\nAmbientCapabilities=",
        ),
        helper_unit.replace(
            "AmbientCapabilities=", "CapabilityBoundingSet=\nAmbientCapabilities="
        ),
    ):
        helper_path.write_text(broadened)
        with pytest.raises(
            VERIFY_MODULE.VerificationError, match="helper capability boundary"
        ):
            VERIFY_MODULE._verify_payload(payload, control, "arm64")
    helper_path.write_text(helper_unit)
    # swapon/swapoff for the managed swap file, and no other widening.
    assert "SystemCallFilter=@swap" in helper_unit.splitlines()
    for widened in (
        helper_unit.replace("SystemCallFilter=@swap", "SystemCallFilter=@swap @mount"),
        helper_unit.replace(
            "SystemCallFilter=@swap", "SystemCallFilter=@swap\nSystemCallFilter=@debug"
        ),
        helper_unit.replace("SystemCallFilter=@swap\n", ""),
    ):
        helper_path.write_text(widened)
        with pytest.raises(
            VERIFY_MODULE.VerificationError, match="helper system-call boundary"
        ):
            VERIFY_MODULE._verify_payload(payload, control, "arm64")
    helper_path.write_text(helper_unit)
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
    package = output / f"vonk-forge-agent_0.1.1_{debian_architecture}.deb"
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
            b"VONK_AGENT_SEMANTIC_VERSION=0.1.1",
            b"VONK_AGENT_SEMANTIC_VERSION=0.1.0",
        )
    )
    agent.chmod(0o555)
    key = tmp_path / "release.pem"
    _release_key(key)
    version = "0.1.0~acceptance.1+g0123456789ab"

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
            "0.1.1",
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
            b"VONK_AGENT_SEMANTIC_VERSION=0.1.1",
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
    deb = output / "vonk-forge-agent_0.1.1_arm64.deb"
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
    deb = output / "vonk-forge-agent_0.1.1_arm64.deb"
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
    version = "0.1.1~dev.1786300000+g0123456789ab"

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


def test_helper_sandbox_lets_dpkg_maintainer_scripts_record_unit_enablement() -> None:
    """dpkg runs deb-systemd-helper inside the helper unit's sandbox.

    Catches a strict sandbox that leaves Debian's enablement state read-only,
    which made every upgrade's postinst fail on "Read-only file system" while
    the package itself installed.
    """

    unit = (
        Path(__file__).resolve().parents[2]
        / "packaging/systemd/vonk-forge-package-helper.service"
    ).read_text()
    assert "ProtectSystem=strict" in unit.splitlines()
    writable = {
        path.lstrip("-")
        for line in unit.splitlines()
        if line.startswith("ReadWritePaths=")
        for path in line.partition("=")[2].split()
    }
    assert "/var/lib/systemd/deb-systemd-helper-enabled" in writable
    assert "/var/lib/systemd/deb-systemd-helper-masked" in writable
    assert "/var/lib/systemd" not in writable
