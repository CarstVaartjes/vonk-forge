from __future__ import annotations

import base64
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

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "scripts/build-agent-deb"
VERIFY = ROOT / "scripts/verify-agent-deb"
PREINST = ROOT / "packaging/debian/preinst"
POSTINST = ROOT / "packaging/debian/postinst"
PRERM = ROOT / "packaging/debian/prerm"
RECOVERY_LIFECYCLE = ROOT / "tests/nodes/test_agent_upgrade_recovery_systemd.sh"
DOCKER_FIREWALL = ROOT / "packaging/bin/vonk-forge-docker-firewall"
PACKAGE_BINARIES = ("vonk-agent", "vonk-agent-helper", "vonk-build-egress", "oras")
BUILD_DIGEST = "sha256:" + "b" * 64
REPAIR_NODE_ID = "spk_2818d189042b4c77aefa7796f4befd23"
REPAIR_SOURCE_VERSION = "0.1.0~dev.381+ga122909feaa3"
REPAIR_VERSION = f"{REPAIR_SOURCE_VERSION}+repair.{REPAIR_NODE_ID.replace('_', '')}.1"
REPAIR_BINARY_REVISION = "a122909feaa3b64d7b15371285e727965c3d7e9a"
REPAIR_PACKAGING_REVISION = "f" * 40


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


def _require_git_object(object_name: str) -> None:
    available = subprocess.run(
        ["git", "cat-file", "-e", object_name],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if available.returncode != 0:
        pytest.skip(
            f"pinned repair source is absent from shallow checkout: {object_name}"
        )


def test_repair_source_lookup_trusts_only_the_resolved_repository(monkeypatch) -> None:
    observed: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> bytes:
        observed.extend(argv)
        return b"source\n"

    monkeypatch.setattr(BUILD_MODULE, "run", fake_run)

    assert BUILD_MODULE.source_at_revision("a" * 40, "packaging/debian/preinst") == (
        b"source\n"
    )
    assert observed == [
        "/usr/bin/git",
        "-c",
        f"safe.directory={BUILD_MODULE.ROOT}",
        "show",
        f"{'a' * 40}:packaging/debian/preinst",
    ]


def test_repair_verifier_trusts_only_the_resolved_repository(monkeypatch) -> None:
    observed: list[str] = []

    def fake_command(argv: list[str], **_kwargs: object) -> bytes:
        observed.extend(argv)
        return b"source\n"

    monkeypatch.setattr(VERIFY_MODULE, "command", fake_command)

    assert VERIFY_MODULE._git_source("b" * 40, "packaging/debian/preinst") == (
        b"source\n"
    )
    assert observed == [
        "/usr/bin/git",
        "-c",
        f"safe.directory={VERIFY_MODULE.ROOT}",
        "--no-replace-objects",
        "-C",
        str(VERIFY_MODULE.ROOT),
        "show",
        f"{'b' * 40}:packaging/debian/preinst",
    ]


def _repair_authority(**replacements: str) -> bytes:
    values = {
        "schema_version": "1",
        "node_id": REPAIR_NODE_ID,
        "installed_version": "0.1.0~dev.335+g2eaaf4d9b2b5",
        "source_target_version": REPAIR_SOURCE_VERSION,
        "source_architecture": "arm64",
        "util_linux_version": "2.39.3-9ubuntu6.3",
        **{
            name: hashlib.sha256(name.encode()).hexdigest()
            for name in VERIFY_MODULE.REPAIR_AUTHORITY_FIELDS
            if name.endswith("_sha256")
        },
        **replacements,
    }
    return "".join(
        f"{name}={values[name]}\n" for name in VERIFY_MODULE.REPAIR_AUTHORITY_FIELDS
    ).encode("ascii")


def _package_members(*, repair: bool) -> dict[str, tarfile.TarInfo]:
    required = set(VERIFY_MODULE.REQUIRED_PAYLOAD)
    executable = set(VERIFY_MODULE.REQUIRED_EXECUTABLE)
    if repair:
        required.difference_update(VERIFY_MODULE.REPAIR_ELF_REMOVALS)
        executable.difference_update(VERIFY_MODULE.REPAIR_ELF_REMOVALS)
        required.update(VERIFY_MODULE.REPAIR_PAYLOAD_ADDITIONS)
        executable.add(VERIFY_MODULE.REPAIR_STANDARD_RUNNER)
    members = {}
    for name in required:
        member = tarfile.TarInfo(name)
        member.type = tarfile.REGTYPE
        member.uid = 0
        member.gid = 0
        member.mode = 0o555 if name in executable else 0o644
        members[name] = member
    return members


def _repair_evidence_documents() -> tuple[dict[str, object], dict[str, str]]:
    expected = {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in (
            "oras",
            "vonk-agent",
            "vonk-agent-helper",
            "vonk-forge-docker-firewall",
            "vonk-forge-package-upgrade-recover",
            "vonk-forge-package-upgrade-recover.standard",
        )
    }
    repository = "https://github.com/CarstVaartjes/vonk-forge"
    packaging_names = {
        "vonk-forge-docker-firewall",
        "vonk-forge-package-upgrade-recover",
    }
    cyclone_components = []
    spdx_packages = []
    for name in expected:
        revision = (
            REPAIR_PACKAGING_REVISION
            if name in packaging_names
            else REPAIR_BINARY_REVISION
        )
        cyclone_components.append(
            {
                "name": name,
                "type": "application",
                "properties": [
                    {
                        "name": (
                            "vonk:packagingSourceRevision"
                            if name in packaging_names
                            else "vonk:binarySourceRevision"
                        ),
                        "value": revision,
                    }
                ],
            }
        )
        spdx_packages.append(
            {
                "name": name,
                "primaryPackagePurpose": "APPLICATION",
                "externalRefs": [
                    {
                        "referenceCategory": "OTHER",
                        "referenceLocator": f"{repository}@{revision}",
                        "referenceType": (
                            "vonk-repair-packaging-source"
                            if name in packaging_names
                            else "vonk-target-binary-source"
                        ),
                    }
                ],
            }
        )
    authority_sha256 = hashlib.sha256(_repair_authority()).hexdigest()
    documents = {
        "provenance.json": {
            "predicate": {
                "buildDefinition": {
                    "externalParameters": {
                        "repair": {
                            "authority_sha256": authority_sha256,
                            "binary_source_revision": REPAIR_BINARY_REVISION,
                            "node_id": REPAIR_NODE_ID,
                            "packaging_source_revision": REPAIR_PACKAGING_REVISION,
                        }
                    },
                    "resolvedDependencies": [
                        {
                            "digest": {"gitCommit": REPAIR_PACKAGING_REVISION},
                            "relationship": "repair-package-source",
                            "uri": repository,
                        },
                        {
                            "digest": {"gitCommit": REPAIR_BINARY_REVISION},
                            "relationship": "target-binary-source",
                            "uri": repository,
                        },
                    ],
                },
                "runDetails": {"builder": {"id": repository}},
            }
        },
        "sbom.cdx.json": {
            "components": cyclone_components,
            "metadata": {
                "component": {
                    "properties": [
                        {
                            "name": "vonk:repairAuthoritySha256",
                            "value": authority_sha256,
                        },
                        {"name": "vonk:repairNodeId", "value": REPAIR_NODE_ID},
                        {
                            "name": "vonk:repairPackagingSourceRevision",
                            "value": REPAIR_PACKAGING_REVISION,
                        },
                    ]
                }
            },
        },
        "sbom.spdx.json": {
            "documentComment": (
                "Vonk repair "
                f"authority_sha256={authority_sha256} "
                f"node_id={REPAIR_NODE_ID} "
                f"binary_source_revision={REPAIR_BINARY_REVISION} "
                f"packaging_source_revision={REPAIR_PACKAGING_REVISION}"
            ),
            "packages": spdx_packages,
        },
    }
    return documents, expected


def _exact_repair_script_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, bytes]:
    source_root = tmp_path / "source"
    payload = tmp_path / "payload"
    control = tmp_path / "control"
    (source_root / "packaging/debian").mkdir(parents=True)
    control.mkdir()
    probe = control / VERIFY_MODULE.REPAIR_PROBE_CONTROL
    _elf_fixture(probe, b"repair-probe")
    probe.chmod(0o755)
    standard_template = (
        b"#!/bin/sh\n"
        b"target_version='@VERSION@'\n"
        b"target_architecture='@ARCHITECTURE@'\n"
        b"target_agent_sha256='@AGENT_SHA256@'\n"
        b"target_helper_sha256='@HELPER_SHA256@'\n"
        b"recovery_unit_sha256='@RECOVERY_UNIT_SHA256@'\n"
        b"recovery_unit_base64='@RECOVERY_UNIT_BASE64@'\n"
        b"agent_gate_sha256='@AGENT_GATE_SHA256@'\n"
        b"agent_gate_base64='@AGENT_GATE_BASE64@'\n"
    )
    repair_template = (
        b"#!/bin/sh\n# VONK_REPAIR_DISPATCH_V1\n"
        b"target_version='@VERSION@'\n"
        b"target_architecture='@ARCHITECTURE@'\n"
        b"target_agent_sha256='@AGENT_SHA256@'\n"
        b"target_helper_sha256='@HELPER_SHA256@'\n"
        b"authority_sha256='@REPAIR_AUTHORITY_SHA256@'\n"
        b"authority_base64='@REPAIR_AUTHORITY_BASE64@'\n"
        b"standard_runner_sha256='@STANDARD_RUNNER_SHA256@'\n"
        b"standard_runner_base64='@STANDARD_RUNNER_BASE64@'\n"
        b"target_agent_unit_sha256='@TARGET_AGENT_UNIT_SHA256@'\n"
        b"target_helper_unit_sha256='@TARGET_HELPER_UNIT_SHA256@'\n"
        b"target_helper_socket_sha256='@TARGET_HELPER_SOCKET_SHA256@'\n"
        b"repair_probe_sha256='@REPAIR_PROBE_SHA256@'\n"
        b"source_capsule_expected='@SOURCE_CAPSULE_V2@'\n"
        b"source_capsule_unit_sha256_expected='@SOURCE_CAPSULE_UNIT_SHA256@'\n"
        b"source_capsule_gate_sha256_expected='@SOURCE_CAPSULE_GATE_SHA256@'\n"
        b"source_capsule_suppression_sha256_expected='@SOURCE_CAPSULE_SUPPRESSION_SHA256@'\n"
    )
    postinst_template = (
        b"#!/bin/sh\n# VONK_REPAIR_POSTINST_V1\n"
        b"# VONK_FORGE_PACKAGE_REPAIR_NONCE\n"
        b"# package-repair.receipt package-repair-helper.receipt\n"
        b"package_version='@VERSION@'\n"
        b"authority_sha256='@REPAIR_AUTHORITY_SHA256@'\n"
        b"observation_receipt_private=/var/lib/vonk-forge/helper/observation-receipt.pk8\n"
        b"observation_receipt_public=/etc/vonk-forge-agent/observation-receipt.pub\n"
        b"# openssl genpkey -algorithm ED25519 -outform DER\n"
        b"# openssl pkey -inform DER; tail -c 32; root:vonk-agent:640:1\n"
        b"ensure_observation_receipt_key() { :; }\n"
    )
    control_template = (
        b"Package: vonk-forge-agent\nVersion: @VERSION@\n"
        b"Architecture: @ARCHITECTURE@\n"
        b"Depends: acl, iproute2, iptables, podman, uidmap\n"
    )
    prerm = (
        b"#!/bin/sh\n# helper-upgrade.pending helper-upgrade.receipt\n"
        b"# safe_pending safe_receipt\n"
        b"# /usr/bin/sync -f /var/lib/vonk-forge\n"
    )
    (source_root / "packaging/debian/preinst").write_bytes(standard_template)
    (source_root / "packaging/debian/preinst-repair").write_bytes(repair_template)
    (source_root / "packaging/debian/postinst-repair").write_bytes(postinst_template)
    (source_root / "packaging/debian/control").write_bytes(control_template)
    (source_root / "packaging/debian/prerm").write_bytes(prerm)
    files = {
        "usr/lib/vonk-forge/vonk-agent": b"agent",
        "usr/lib/vonk-forge/vonk-agent-helper": b"helper",
        "lib/systemd/system/vonk-forge-package-upgrade-recover.service": b"recover-unit",
        "lib/systemd/system/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf": b"agent-gate",
        "lib/systemd/system/vonk-forge-agent.service": b"agent-unit",
        "lib/systemd/system/vonk-forge-package-helper.service": b"helper-unit",
        "lib/systemd/system/vonk-forge-package-helper.socket": b"helper-socket",
    }
    for relative, raw in files.items():
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    monkeypatch.setattr(VERIFY_MODULE, "ROOT", source_root)
    source_git_files = {
        "packaging/debian/preinst": standard_template,
        "packaging/debian/preinst-repair": repair_template,
        "packaging/debian/postinst-repair": postinst_template,
        "packaging/debian/control": control_template,
        "packaging/debian/prerm": prerm,
        "packaging/bin/vonk-forge-docker-firewall": b"firewall-helper",
        "packaging/systemd/vonk-forge-package-upgrade-recover.service": b"recover-unit",
        "packaging/systemd/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf": b"agent-gate",
    }

    def git_source(revision: str, relative: str) -> bytes:
        expected_revision = (
            REPAIR_PACKAGING_REVISION
            if relative
            in {
                "packaging/debian/preinst-repair",
                "packaging/debian/postinst-repair",
                "packaging/debian/control",
                "packaging/debian/prerm",
                "packaging/bin/vonk-forge-docker-firewall",
            }
            else REPAIR_BINARY_REVISION
        )
        assert revision == expected_revision
        return source_git_files[relative]

    monkeypatch.setattr(VERIFY_MODULE, "_git_source", git_source)
    standard = VERIFY_MODULE._render_packaging_source(
        "packaging/debian/preinst",
        (
            (b"@VERSION@", REPAIR_SOURCE_VERSION.encode()),
            (b"@ARCHITECTURE@", b"arm64"),
            (b"@AGENT_SHA256@", hashlib.sha256(b"agent").hexdigest().encode()),
            (b"@HELPER_SHA256@", hashlib.sha256(b"helper").hexdigest().encode()),
            (
                b"@RECOVERY_UNIT_SHA256@",
                hashlib.sha256(b"recover-unit").hexdigest().encode(),
            ),
            (b"@RECOVERY_UNIT_BASE64@", base64.b64encode(b"recover-unit")),
            (
                b"@AGENT_GATE_SHA256@",
                hashlib.sha256(b"agent-gate").hexdigest().encode(),
            ),
            (b"@AGENT_GATE_BASE64@", base64.b64encode(b"agent-gate")),
        ),
    )
    authority = _repair_authority(
        source_agent_sha256=hashlib.sha256(b"agent").hexdigest(),
        source_helper_sha256=hashlib.sha256(b"helper").hexdigest(),
        source_runner_sha256=hashlib.sha256(standard).hexdigest(),
        source_unit_sha256=hashlib.sha256(b"recover-unit").hexdigest(),
        source_agent_gate_sha256=hashlib.sha256(b"agent-gate").hexdigest(),
        repair_probe_sha256=hashlib.sha256(probe.read_bytes()).hexdigest(),
    )
    authority_sha256 = hashlib.sha256(authority).hexdigest().encode()
    repair = VERIFY_MODULE._render_packaging_source(
        "packaging/debian/preinst-repair",
        (
            (b"@VERSION@", REPAIR_VERSION.encode()),
            (b"@ARCHITECTURE@", b"arm64"),
            (b"@AGENT_SHA256@", hashlib.sha256(b"agent").hexdigest().encode()),
            (b"@HELPER_SHA256@", hashlib.sha256(b"helper").hexdigest().encode()),
            (b"@REPAIR_AUTHORITY_SHA256@", authority_sha256),
            (b"@REPAIR_AUTHORITY_BASE64@", base64.b64encode(authority)),
            (
                b"@REPAIR_PROBE_SHA256@",
                hashlib.sha256(probe.read_bytes()).hexdigest().encode(),
            ),
            (
                b"@STANDARD_RUNNER_SHA256@",
                hashlib.sha256(standard).hexdigest().encode(),
            ),
            (b"@STANDARD_RUNNER_BASE64@", base64.b64encode(standard)),
            (
                b"@TARGET_AGENT_UNIT_SHA256@",
                hashlib.sha256(b"agent-unit").hexdigest().encode(),
            ),
            (
                b"@TARGET_HELPER_UNIT_SHA256@",
                hashlib.sha256(b"helper-unit").hexdigest().encode(),
            ),
            (
                b"@TARGET_HELPER_SOCKET_SHA256@",
                hashlib.sha256(b"helper-socket").hexdigest().encode(),
            ),
            (b"@SOURCE_CAPSULE_V2@", b"0"),
            (b"@SOURCE_CAPSULE_UNIT_SHA256@", b"0" * 64),
            (b"@SOURCE_CAPSULE_GATE_SHA256@", b"0" * 64),
            (b"@SOURCE_CAPSULE_SUPPRESSION_SHA256@", b"0" * 64),
        ),
    )
    postinst = VERIFY_MODULE._render_packaging_source(
        "packaging/debian/postinst-repair",
        (
            (b"@VERSION@", REPAIR_VERSION.encode()),
            (b"@REPAIR_AUTHORITY_SHA256@", authority_sha256),
        ),
    )
    for relative, raw in (
        (VERIFY_MODULE.REPAIR_STANDARD_RUNNER, standard),
        ("usr/lib/vonk-forge/vonk-forge-package-upgrade-recover", repair),
    ):
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (control / "preinst").write_bytes(repair)
    (control / "postinst").write_bytes(postinst)
    return payload, control, authority


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
    if path.name == "vonk-build-egress":
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

    with pytest.raises(BUILD_MODULE.BuildError, match="is not static"):
        BUILD_MODULE.read_release_binary(path, machine=183, require_static=True)


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


@pytest.mark.skip(reason="superseded by durable package recovery")
def test_package_helper_upgrade_bridge_is_narrow_bounded_and_retryable() -> None:
    preinst = PREINST.read_text()
    postinst = POSTINST.read_text()
    helper = (ROOT / "packaging/systemd/vonk-forge-package-helper.service").read_text()
    exact_paths = "ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent"

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
    bridge_decision = preinst.index("# Development packages predating this bridge")
    assert preunpack_gate < bridge_decision
    assert preinst.index('/usr/bin/sync -f "$pending_new"') < preunpack_gate
    assert preinst.index("/usr/bin/sync -f /var/lib/vonk-forge") < preunpack_gate
    assert preinst.index("package_action=1") < preunpack_gate
    assert preinst.index("inside_helper=0") < preunpack_gate
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
    assert "keyrings=/usr/share/keyrings" in preinst
    assert "package_doc=/usr/share/doc/vonk-forge-agent" in preinst
    assert '"$keyrings/.vonk-package-write.XXXXXX"' in preinst
    assert '"$package_doc/.vonk-package-write.XXXXXX"' in preinst
    assert preinst.count("helper_namespace_has_package_paths") >= 3
    acceptance = preinst.index('if [ "$helper_main_pid" != "$previous_main_pid" ]')
    acceptance_end = preinst.index("exit 0", acceptance)
    acceptance_block = preinst[acceptance:acceptance_end]
    assert "bridge_dropin_is_safe" in acceptance_block
    assert "helper_has_effective_bridge_paths" in acceptance_block
    assert "helper_namespace_has_package_paths" in acceptance_block
    assert "package_action=1\n    old_version=${2:-}" in preinst
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
        "[ $((0$mode & 0022)) -eq 0 ]",
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
    helper = (ROOT / "packaging/systemd/vonk-forge-package-helper.service").read_text()
    helper_socket = (
        ROOT / "packaging/systemd/vonk-forge-package-helper.socket"
    ).read_text()

    assert (
        "/lib/systemd/system"
        in next(
            line
            for line in helper.splitlines()
            if line.startswith("ReadWritePaths=/var/lib/vonk-forge ")
        ).split()
    )
    assert "DirectoryMode=0711" in helper_socket.splitlines()
    assert "RuntimeDirectory=vonk-forge-package-candidates" in helper.splitlines()
    assert (
        "bridge_dropin_dir=/lib/systemd/system/vonk-forge-package-helper.service.d"
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


def test_recovery_binds_exact_dev335_dpkg_invocation_and_candidate() -> None:
    preinst = PREINST.read_text()

    assert '"$(/usr/bin/readlink -f "/proc/$PPID/exe")" = /usr/bin/dpkg' in preinst
    assert "= --install" in preinst
    assert "= --force-confold" in preinst
    assert "/var/lib/vonk-forge/incoming/[0-9a-f]*.deb" in preinst
    assert "expected_candidate_metadata=$agent_uid:$agent_gid:600:1" in preinst
    assert "custody_root=/run/vonk-forge-package-candidates" in preinst
    assert '[ "${#invocation}" -eq 32 ]' in preinst
    assert "expected_candidate_metadata=0:0:600:1" in preinst
    assert 'safe_root_directory "$invocation_dir" 700' in preinst
    assert "candidate_before=" in preinst and "candidate_after=" in preinst
    assert "helper_namespace_has_package_paths" in preinst
    assert "inherited helper sandbox lacks package lifecycle paths" in preinst
    namespace_probe = preinst.index("if ! helper_namespace_has_package_paths")
    recovery_arm = preinst.index("arm_controller_recovery ||")
    assert namespace_probe < recovery_arm
    assert "Bootstrap trust boundary" in preinst
    assert "old-protocol TOCTOU" in preinst


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
    assert "safe_recovery_capsule || unsafe_state 'recovery capsule'" in preinst
    assert "safe_capsule_enablement" in preinst
    assert (
        'if [ -e "$embedded_destination" ] || [ -L "$embedded_destination" ]' in preinst
    )
    intent_retire = preinst.index('/usr/bin/rm -- "$intent"')
    capsule_retire = preinst.index(
        "retire_recovery_capsule || unsafe_state", intent_retire
    )
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
    assert "reason=exact_identity_proven" in lifecycle
    assert "package-upgrade.status" in lifecycle


def test_root_custody_lifecycle_executes_the_exact_real_dpkg_contract() -> None:
    lifecycle = RECOVERY_LIFECYCLE.read_text()

    assert "candidate_custody=${CANDIDATE_CUSTODY:-legacy}" in lifecycle
    assert "custody_root=/run/vonk-forge-package-candidates" in lifecycle
    assert "custody_invocation=0123456789abcdef0123456789abcdef" in lifecycle
    assert (
        "candidate=$custody_root/$custody_invocation/$package_digest.deb" in lifecycle
    )
    assert "helper_runtime_directory=vonk-forge-package-candidates" in lifecycle
    assert "helper_runtime_mode=0700" in lifecycle
    assert "helper_runtime_preserve=restart" in lifecycle
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
        '"$observation_receipt_private"',
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
    assert lifecycle.count("assert_interrupted_baseline_state") == 4
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
    assert (
        crash_window.count('done < "/proc/$helper_pid/status" 2>/dev/null || continue')
        == 2
    )
    assert 'done < "/proc/$dpkg_pid/status" 2>/dev/null || exit 1' in crash_window
    assert "crash_intent_digest" in crash_window
    post_kill = lifecycle[final_kill:]
    assert "helper_main_pid_after" in post_kill
    assert 'test "$helper_active_state" = failed' in post_kill
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
    assert dpkg_only.count('"$helper_unit")" = frozen') == 1
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
    assert post_watcher.count("test -f /var/lib/vonk-forge/package-upgrade/intent") == 1
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
        'systemctl --system start "$recovery_unit"',
    ):
        assert durable_proof in post_remove
    assert (
        '"$recovery_load_state" == not-found'
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
    assert lifecycle.index('test "$recovery_load_state" = not-found', convergence_break)
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
    assert "journalctl --system --no-pager -n 200" in lifecycle
    assert "firewall_fixture=/run/systemd/system/$firewall_unit" in lifecycle
    assert "Vonk Forge package recovery firewall fixture" in lifecycle
    assert lifecycle.count('"$firewall_unit"') >= 7
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
    assert '"$observation_receipt_private"' in cleanup
    assert '"$observation_receipt_public"' in cleanup
    assert cleanup.count("dpkg-query --show vonk-forge-agent") == 2
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
    receipt_write = postinst.index('"helper_main_pid=$new_helper_pid"')
    receipt_file_sync = postinst.index('/usr/bin/sync -f "$receipt_new"', receipt_write)
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
    schedule = postinst.index('schedule_permanent_helper_restart "$PPID" "$dpkg_start"')
    assert pending_probe < finisher_decision < pending_write < schedule
    assert (
        "interrupted helper activation requires a live systemd configure retry"
        in postinst
    )
    assert "pending helper upgrade state is unsafe" in postinst

    stop_finisher = prerm.index("vonk-forge-package-helper-upgrade-finish.service")
    refuse_durable = prerm.index(
        "cannot remove package during durable upgrade recovery"
    )
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


def test_repair_version_grammar_is_disjoint_node_bound_and_sequenced() -> None:
    assert VERIFY_MODULE.PACKAGE_VERSION.fullmatch(REPAIR_VERSION) is None
    match = VERIFY_MODULE.REPAIR_PACKAGE_VERSION.fullmatch(REPAIR_VERSION)
    assert match is not None
    assert match.group("source") == REPAIR_SOURCE_VERSION
    assert match.group("node") == REPAIR_NODE_ID.removeprefix("spk_")
    assert (
        VERIFY_MODULE.REPAIR_PACKAGE_VERSION.fullmatch(
            REPAIR_VERSION.rsplit(".", 1)[0] + ".0"
        )
        is None
    )


def test_repair_cli_requires_explicit_exact_expectations(tmp_path: Path) -> None:
    missing = tmp_path / "missing.deb"
    without_expectations = subprocess.run(
        [VERIFY, "--json", "--repair", missing],
        capture_output=True,
        text=True,
        check=False,
    )
    without_mode = subprocess.run(
        [
            VERIFY,
            "--json",
            "--expected-node-id",
            REPAIR_NODE_ID,
            "--expected-repair-authority-sha256",
            "a" * 64,
            missing,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert without_expectations.returncode == 1
    assert "requires exact node, authority, release-key, and source" in (
        without_expectations.stdout
    )
    assert without_mode.returncode == 1
    assert "require explicit repair mode" in without_mode.stdout


def test_repair_release_key_requires_external_trusted_fingerprint(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload"
    keyring = payload / "usr/share/keyrings"
    keyring.mkdir(parents=True)
    public_key = bytes(range(32))
    (keyring / "vonk-forge-release.pub").write_text(public_key.hex() + "\n")

    expected = hashlib.sha256(public_key).hexdigest()
    assert VERIFY_MODULE._release_public_key(payload, expected) == public_key
    with pytest.raises(VERIFY_MODULE.VerificationError, match="external authority"):
        VERIFY_MODULE._release_public_key(payload, "f" * 64)


def test_default_and_repair_payload_sets_are_strictly_disjoint() -> None:
    ordinary = _package_members(repair=False)
    repair = _package_members(repair=True)

    VERIFY_MODULE._verify_members(ordinary)
    VERIFY_MODULE._verify_members(repair, repair=True)
    with pytest.raises(
        VERIFY_MODULE.VerificationError, match="package payload is incomplete"
    ):
        VERIFY_MODULE._verify_members(repair)
    with pytest.raises(VERIFY_MODULE.VerificationError, match="payload is incomplete"):
        VERIFY_MODULE._verify_members(ordinary, repair=True)


def test_repair_evidence_omits_only_the_unbound_build_egress_binary() -> None:
    ordinary = VERIFY_MODULE._evidence_executables(repair=False)
    repair = VERIFY_MODULE._evidence_executables(repair=True)

    assert ordinary - repair == VERIFY_MODULE.REPAIR_ELF_REMOVALS
    assert repair - ordinary == {VERIFY_MODULE.REPAIR_STANDARD_RUNNER}
    assert "usr/lib/vonk-forge/vonk-build-egress" not in repair


def test_ordinary_control_verification_remains_unchanged(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir()
    for name in VERIFY_MODULE.REQUIRED_CONTROL:
        source = ROOT / f"packaging/debian/{name}"
        path = control / name
        path.write_bytes(source.read_bytes() if source.is_file() else b"")
        path.chmod(0o755 if name in {"preinst", "postinst", "prerm"} else 0o644)

    VERIFY_MODULE._verify_control(control)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("extra-postrm", "control archive is not exact"),
        ("append-prerm", "prerm bytes are not exact"),
        ("control-replaces", "control metadata is not exact"),
        ("probe-name", "control archive is not exact"),
        ("probe-uid", "control archive is not exact"),
        ("probe-gid", "control archive is not exact"),
        ("probe-mode", "control archive is not exact"),
        ("probe-symlink", "control archive is not exact"),
        ("probe-hardlink", "control archive is not exact"),
        ("probe-truncated", "expected ELF"),
        ("probe-class", "expected ELF"),
        ("probe-endian", "expected ELF"),
        ("probe-machine", "expected ELF"),
    ),
)
def test_repair_control_archive_is_exact_packaging_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    _, control, _ = _exact_repair_script_fixture(tmp_path, monkeypatch)
    archive_members = {}
    for name in VERIFY_MODULE.REQUIRED_CONTROL | {VERIFY_MODULE.REPAIR_PROBE_CONTROL}:
        member = tarfile.TarInfo(name)
        member.type = tarfile.REGTYPE
        member.uid = 0
        member.gid = 0
        member.mode = (
            0o755
            if name
            in {"preinst", "postinst", "prerm", VERIFY_MODULE.REPAIR_PROBE_CONTROL}
            else 0o644
        )
        archive_members[name] = member
    control_template = VERIFY_MODULE._git_source(
        REPAIR_PACKAGING_REVISION, "packaging/debian/control"
    )
    (control / "control").write_bytes(
        VERIFY_MODULE._render_source(
            control_template,
            "control",
            (
                (b"@VERSION@", REPAIR_VERSION.encode()),
                (b"@ARCHITECTURE@", b"arm64"),
            ),
        )
    )
    (control / "conffiles").write_text(
        "/etc/vonk-forge-agent/containers-storage.conf\n"
    )
    (control / "md5sums").write_text("")
    (control / "prerm").write_bytes(
        VERIFY_MODULE._git_source(REPAIR_PACKAGING_REVISION, "packaging/debian/prerm")
    )
    for name in ("preinst", "postinst", "prerm"):
        (control / name).chmod(0o755)
    for name in ("control", "conffiles", "md5sums"):
        (control / name).chmod(0o644)

    VERIFY_MODULE._verify_control(
        control,
        archive_members=archive_members,
        repair=True,
        expected_packaging_source_revision=REPAIR_PACKAGING_REVISION,
        architecture="arm64",
        version=REPAIR_VERSION,
    )
    if mutation == "extra-postrm":
        (control / "postrm").write_text("#!/bin/sh\nexit 0\n")
        extra = tarfile.TarInfo("postrm")
        extra.type = tarfile.REGTYPE
        extra.uid = 0
        extra.gid = 0
        extra.mode = 0o755
        archive_members["postrm"] = extra
    elif mutation == "append-prerm":
        (control / "prerm").write_bytes(
            (control / "prerm").read_bytes() + b"\nexit 0\n"
        )
    elif mutation == "control-replaces":
        (control / "control").write_bytes(
            (control / "control").read_bytes() + b"Replaces: arbitrary-root-package\n"
        )
    elif mutation == "probe-name":
        probe = control / VERIFY_MODULE.REPAIR_PROBE_CONTROL
        dotless = "vonk-repair-helper-probe"
        probe.rename(control / dotless)
        archive_members[dotless] = archive_members.pop(
            VERIFY_MODULE.REPAIR_PROBE_CONTROL
        )
    elif mutation == "probe-uid":
        archive_members[VERIFY_MODULE.REPAIR_PROBE_CONTROL].uid = 1
    elif mutation == "probe-gid":
        archive_members[VERIFY_MODULE.REPAIR_PROBE_CONTROL].gid = 1
    elif mutation == "probe-mode":
        archive_members[VERIFY_MODULE.REPAIR_PROBE_CONTROL].mode = 0o644
    elif mutation == "probe-symlink":
        probe = control / VERIFY_MODULE.REPAIR_PROBE_CONTROL
        probe.unlink()
        probe.symlink_to(control / "preinst")
    elif mutation == "probe-hardlink":
        probe = control / VERIFY_MODULE.REPAIR_PROBE_CONTROL
        probe.unlink()
        os.link(control / "preinst", probe)
    else:
        probe = control / VERIFY_MODULE.REPAIR_PROBE_CONTROL
        raw = bytearray(probe.read_bytes())
        if mutation == "probe-truncated":
            raw = raw[:4]
        elif mutation == "probe-class":
            raw[4] = 1
        elif mutation == "probe-endian":
            raw[5] = 2
        else:
            struct.pack_into("<H", raw, 18, 62)
        probe.write_bytes(raw)
    with pytest.raises(VERIFY_MODULE.VerificationError, match=message):
        VERIFY_MODULE._verify_control(
            control,
            archive_members=archive_members,
            repair=True,
            expected_packaging_source_revision=REPAIR_PACKAGING_REVISION,
            architecture="arm64",
            version=REPAIR_VERSION,
        )


def test_control_archive_rejects_duplicate_path_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in ("./control", "control"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.REGTYPE
            member.uid = 0
            member.gid = 0
            member.mode = 0o644
            member.size = 0
            archive.addfile(member, io.BytesIO())
    monkeypatch.setattr(
        VERIFY_MODULE, "command", lambda *args, **kwargs: stream.getvalue()
    )

    with pytest.raises(VERIFY_MODULE.VerificationError, match="member is unsafe"):
        VERIFY_MODULE._control_members(tmp_path / "repair.deb")


@pytest.mark.parametrize(
    ("mutation", "architecture", "version", "expected_node", "expected_message"),
    (
        ("order", "arm64", REPAIR_VERSION, REPAIR_NODE_ID, "not canonical"),
        ("node", "arm64", REPAIR_VERSION, "spk_" + "9" * 32, "not canonical"),
        (
            "version",
            "arm64",
            REPAIR_VERSION.replace("381", "382", 1),
            REPAIR_NODE_ID,
            "not canonical",
        ),
        ("architecture", "amd64", REPAIR_VERSION, REPAIR_NODE_ID, "not canonical"),
    ),
)
def test_repair_authority_rejects_field_order_node_version_and_architecture(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    architecture: str,
    version: str,
    expected_node: str,
    expected_message: str,
) -> None:
    raw = _repair_authority()
    if mutation == "order":
        lines = raw.splitlines(keepends=True)
        lines[0], lines[1] = lines[1], lines[0]
        raw = b"".join(lines)
    monkeypatch.setattr(VERIFY_MODULE, "command", lambda *args, **kwargs: b"")

    with pytest.raises(VERIFY_MODULE.VerificationError, match=expected_message):
        VERIFY_MODULE._read_repair_authority(
            raw,
            architecture=architecture,
            version=version,
            expected_node_id=expected_node,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_repair_authority_requires_the_exact_expected_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _repair_authority()
    monkeypatch.setattr(VERIFY_MODULE, "command", lambda *args, **kwargs: b"")

    with pytest.raises(VERIFY_MODULE.VerificationError, match="SHA-256"):
        VERIFY_MODULE._read_repair_authority(
            raw,
            architecture="arm64",
            version=REPAIR_VERSION,
            expected_node_id=REPAIR_NODE_ID,
            expected_sha256="0" * 64,
        )


def test_repair_authority_accepts_only_the_frozen_canonical_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _repair_authority()
    monkeypatch.setattr(VERIFY_MODULE, "command", lambda *args, **kwargs: b"")

    document = VERIFY_MODULE._read_repair_authority(
        raw,
        architecture="arm64",
        version=REPAIR_VERSION,
        expected_node_id=REPAIR_NODE_ID,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )

    assert tuple(document) == VERIFY_MODULE.REPAIR_AUTHORITY_FIELDS
    assert document["node_id"] == REPAIR_NODE_ID


@pytest.mark.parametrize("changed", ("vonk-agent", "vonk-agent-helper"))
def test_repair_payload_binaries_must_match_source_authority(
    tmp_path: Path, changed: str
) -> None:
    payload = tmp_path / "payload"
    binaries = {
        "vonk-agent": b"exact source agent",
        "vonk-agent-helper": b"exact source helper",
    }
    for name, raw in binaries.items():
        path = payload / "usr/lib/vonk-forge" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    authority = {
        "source_agent_sha256": hashlib.sha256(binaries["vonk-agent"]).hexdigest(),
        "source_helper_sha256": hashlib.sha256(
            binaries["vonk-agent-helper"]
        ).hexdigest(),
    }
    VERIFY_MODULE._verify_repair_binary_authority(payload, authority)
    changed_path = payload / "usr/lib/vonk-forge" / changed
    changed_path.write_bytes(changed_path.read_bytes() + b"\nsubstitution")
    with pytest.raises(VERIFY_MODULE.VerificationError, match="source authority"):
        VERIFY_MODULE._verify_repair_binary_authority(payload, authority)


def test_repair_probe_bytes_must_match_the_authority_and_embedded_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, control, authority = _exact_repair_script_fixture(tmp_path, monkeypatch)
    authority_document = dict(
        line.split("=", 1) for line in authority.decode("ascii").splitlines()
    )
    probe = control / VERIFY_MODULE.REPAIR_PROBE_CONTROL
    probe_raw = probe.read_bytes()

    probe.write_bytes(probe_raw + b"\nsubstituted-probe")
    with pytest.raises(
        VERIFY_MODULE.VerificationError, match="probe does not match authority"
    ):
        VERIFY_MODULE._verify_exact_repair_scripts(
            payload,
            control,
            architecture="arm64",
            version=REPAIR_VERSION,
            authority_raw=authority,
            authority=authority_document,
            expected_binary_source_revision=REPAIR_BINARY_REVISION,
            expected_packaging_source_revision=REPAIR_PACKAGING_REVISION,
        )

    probe.write_bytes(probe_raw)
    runner = (control / "preinst").read_bytes()
    with pytest.raises(
        VERIFY_MODULE.VerificationError,
        match="repair capsule repair_probe_sha256 is invalid",
    ):
        VERIFY_MODULE._verify_digest_assignment(
            runner.replace(
                authority_document["repair_probe_sha256"].encode("ascii"),
                b"0" * 64,
                1,
            ),
            "repair_probe_sha256",
            probe_raw,
        )


def test_repair_firewall_must_match_packaging_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload"
    firewall = payload / "usr/lib/vonk-forge/vonk-forge-docker-firewall"
    firewall.parent.mkdir(parents=True)
    firewall.write_bytes(b"exact packaging firewall")
    monkeypatch.setattr(
        VERIFY_MODULE,
        "_git_source",
        lambda revision, relative: (
            b"exact packaging firewall"
            if revision == REPAIR_PACKAGING_REVISION
            and relative == "packaging/bin/vonk-forge-docker-firewall"
            else b"wrong source"
        ),
    )
    VERIFY_MODULE._verify_repair_firewall_source(payload, REPAIR_PACKAGING_REVISION)
    firewall.write_bytes(b"substituted root firewall")
    with pytest.raises(VERIFY_MODULE.VerificationError, match="packaging source"):
        VERIFY_MODULE._verify_repair_firewall_source(payload, REPAIR_PACKAGING_REVISION)


@pytest.mark.parametrize(
    ("target", "suffix"),
    (
        ("preinst", b"\nunsafe_state() { return 0; }\n"),
        ("installed", b"\nexit 0\n"),
        ("standard", b"\nallow_agent_start() { return 0; }\n"),
        ("postinst", b"\n# root behavior override\nexit 0\n"),
    ),
)
def test_repair_verifier_rejects_any_maintainer_script_append_or_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    suffix: bytes,
) -> None:
    payload, control, authority = _exact_repair_script_fixture(tmp_path, monkeypatch)
    authority_document = dict(
        line.split("=", 1) for line in authority.decode("ascii").splitlines()
    )
    VERIFY_MODULE._verify_exact_repair_scripts(
        payload,
        control,
        architecture="arm64",
        version=REPAIR_VERSION,
        authority_raw=authority,
        authority=authority_document,
        expected_binary_source_revision=REPAIR_BINARY_REVISION,
        expected_packaging_source_revision=REPAIR_PACKAGING_REVISION,
    )
    path = {
        "preinst": control / "preinst",
        "installed": (
            payload / "usr/lib/vonk-forge/vonk-forge-package-upgrade-recover"
        ),
        "standard": payload / VERIFY_MODULE.REPAIR_STANDARD_RUNNER,
        "postinst": control / "postinst",
    }[target]
    path.write_bytes(path.read_bytes() + suffix)

    with pytest.raises(
        VERIFY_MODULE.VerificationError, match="script bytes are not canonical"
    ):
        VERIFY_MODULE._verify_exact_repair_scripts(
            payload,
            control,
            architecture="arm64",
            version=REPAIR_VERSION,
            authority_raw=authority,
            authority=authority_document,
            expected_binary_source_revision=REPAIR_BINARY_REVISION,
            expected_packaging_source_revision=REPAIR_PACKAGING_REVISION,
        )


@pytest.mark.parametrize(
    ("relative", "message"),
    (
        ("packaging/debian/preinst", "source runner"),
        (
            "packaging/systemd/vonk-forge-package-upgrade-recover.service",
            "source systemd",
        ),
        (
            (
                "packaging/systemd/vonk-forge-agent.service.d/"
                "20-package-upgrade-recovery.conf"
            ),
            "source systemd",
        ),
    ),
)
def test_repair_verifier_rejects_binary_revision_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    message: str,
) -> None:
    payload, control, authority = _exact_repair_script_fixture(tmp_path, monkeypatch)
    authority_document = dict(
        line.split("=", 1) for line in authority.decode("ascii").splitlines()
    )
    original = VERIFY_MODULE._git_source

    def changed_source(revision: str, selected: str) -> bytes:
        raw = original(revision, selected)
        return raw + b"\n# binary source drift\n" if selected == relative else raw

    monkeypatch.setattr(VERIFY_MODULE, "_git_source", changed_source)
    with pytest.raises(VERIFY_MODULE.VerificationError, match=message):
        VERIFY_MODULE._verify_exact_repair_scripts(
            payload,
            control,
            architecture="arm64",
            version=REPAIR_VERSION,
            authority_raw=authority,
            authority=authority_document,
            expected_binary_source_revision=REPAIR_BINARY_REVISION,
            expected_packaging_source_revision=REPAIR_PACKAGING_REVISION,
        )


@pytest.mark.parametrize("relative", tuple(VERIFY_MODULE.NORMAL_SYSTEMD_PAYLOAD))
def test_repair_verifier_rejects_every_changed_normal_systemd_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    source_root = tmp_path / "source"
    payload = tmp_path / "payload"
    monkeypatch.setattr(VERIFY_MODULE, "ROOT", source_root)
    for (
        payload_relative,
        source_relative,
    ) in VERIFY_MODULE.NORMAL_SYSTEMD_PAYLOAD.items():
        source_path = source_root / source_relative
        payload_path = payload / payload_relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(payload_relative.encode())
        payload_path.write_bytes(payload_relative.encode())
    monkeypatch.setattr(
        VERIFY_MODULE,
        "_git_source",
        lambda revision, selected: (
            (source_root / selected).read_bytes()
            if revision == REPAIR_PACKAGING_REVISION
            else b"wrong revision"
        ),
    )
    VERIFY_MODULE._verify_normal_systemd_payload(payload, REPAIR_PACKAGING_REVISION)
    changed = payload / relative
    changed.write_bytes(changed.read_bytes() + b"\n# override\n")

    with pytest.raises(
        VERIFY_MODULE.VerificationError, match="changes ordinary systemd payload"
    ):
        VERIFY_MODULE._verify_normal_systemd_payload(payload, REPAIR_PACKAGING_REVISION)


def test_repair_provenance_requires_exact_source_relationships() -> None:
    documents, expected = _repair_evidence_documents()
    authority_sha256 = hashlib.sha256(_repair_authority()).hexdigest()

    sources = VERIFY_MODULE._verify_repair_evidence(
        documents,
        version=REPAIR_VERSION,
        expected=expected,
        expected_node_id=REPAIR_NODE_ID,
        expected_authority_sha256=authority_sha256,
    )
    assert sources == {
        "binary_source_revision": REPAIR_BINARY_REVISION,
        "packaging_source_revision": REPAIR_PACKAGING_REVISION,
    }
    dependencies = documents["provenance.json"]["predicate"]["buildDefinition"][
        "resolvedDependencies"
    ]
    dependencies[0]["relationship"] = "target-binary-source"
    with pytest.raises(VERIFY_MODULE.VerificationError, match="resolved dependencies"):
        VERIFY_MODULE._verify_repair_evidence(
            documents,
            version=REPAIR_VERSION,
            expected=expected,
            expected_node_id=REPAIR_NODE_ID,
            expected_authority_sha256=authority_sha256,
        )


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


def test_repair_runtime_bounds_the_transient_manager_probe() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    postinst = (ROOT / "packaging/debian/postinst-repair").read_text()

    assert "vonk-forge-package-upgrade-recover.service" in runner
    assert "20-package-upgrade-recovery.conf" in runner
    assert "vonk-forge-package-upgrade-repair.service" not in runner
    assert "30-package-upgrade-repair.conf" not in runner
    assert "/usr/bin/systemd-run --system --wait --pipe --collect --quiet" in runner
    assert "--property=CapabilityBoundingSet=CAP_SYS_PTRACE" in runner
    assert "--property=AmbientCapabilities=" in runner
    assert "--property=PrivateNetwork=yes" in runner
    assert "--property=ProtectSystem=strict" in runner
    assert "--property=ReadOnlyPaths=/" in runner
    assert "--property=SystemCallFilter=@system-service" in runner
    assert "process_vm_readv process_vm_writev pidfd_getfd kcmp" in runner
    assert '"$setpriv_binary" --no-new-privs -- "$repair_probe" probe-helper' in runner
    assert '"$repair_probe" probe-agent' in runner
    manager_probe = runner[
        runner.index("prove_helper_with_manager() {") : runner.index(
            "prove_helper_parent_chain() {"
        )
    ]
    assert "prove_running_agent_exact" not in manager_probe
    assert "--reuid" not in runner
    assert "--regid" not in runner
    assert '[ "$old_agent_groups" = none ]' in runner
    assert '[ "$old_agent_groups" = "$vonk_agent_gid" ]' in runner
    assert "systemctl --system" not in postinst
    assert "package-repair.receipt" not in postinst
    assert "helper-upgrade.pending" not in postinst
    assert "VONK_FORGE_PACKAGE_REPAIR_NONCE" in postinst


@pytest.mark.parametrize(
    "script_path",
    (
        ROOT / "packaging/debian/preinst-repair",
        ROOT / "packaging/debian/postinst-repair",
    ),
)
def test_repair_canonical_line_files_reject_unterminated_trailing_bytes(
    tmp_path: Path, script_path: Path
) -> None:
    script = script_path.read_text()
    helper = script[
        script.index("canonical_line_file() {") : script.index(
            "\n}\n", script.index("canonical_line_file() {")
        )
        + 3
    ]
    document = tmp_path / "document"
    document.write_text("first\nsecond\n", encoding="utf-8")

    exact = subprocess.run(
        ["/bin/sh", "-c", f'{helper}\ncanonical_line_file "$1" 2', "sh", document],
        check=False,
    )
    with document.open("ab") as output:
        output.write(b"x")
    trailing = subprocess.run(
        ["/bin/sh", "-c", f'{helper}\ncanonical_line_file "$1" 2', "sh", document],
        check=False,
    )

    assert exact.returncode == 0
    assert trailing.returncode != 0


def test_repair_blob_staging_preserves_mode_and_repairs_exact_legacy_residue() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    stage = runner[runner.index("stage_blob() {") : runner.index("load_authority() {")]
    delegate = runner[
        runner.index("standard_runner_ready() {") : runner.index("arm_repair() {")
    ]

    assert "stage_blob_mode=$4" in stage
    assert 'chmod "0$stage_blob_mode" "$stage_blob_temporary"' in stage
    assert "\n    mode=$4\n" not in stage
    assert "standard_runner_is_exact_legacy_residue" in delegate
    assert 'safe_root_file "$standard_runner" 755' in delegate
    assert "&& ! standard_runner_is_exact_legacy_residue" in delegate


def test_repair_runtime_binds_every_running_unit_to_uid_and_gid() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    normalized = re.sub(r"\s+", " ", runner.replace("\\\n", " "))

    helper_proof = (
        'prove_running_unit "$helper_unit" "$helper_binary" "$target_helper_sha256" 0 0'
    )
    agent_proof = (
        'prove_running_unit "$agent_unit" "$agent_binary" '
        '"$target_agent_sha256" "$agent_uid" "$agent_gid"'
    )
    assert normalized.count(helper_proof) == 7
    assert normalized.count(agent_proof) == 6
    assert '"$target_helper_sha256" 0)' not in runner
    assert '"$target_agent_sha256" "$agent_uid")' not in runner


def test_repair_manager_probe_uses_the_exact_transient_sandbox_contract() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    start = runner.index("probe_output=$(/usr/bin/systemd-run")
    end = runner.index(") || return 1", start)
    command = runner[start + len("probe_output=$(") : end]
    tokens = shlex.split(command.replace("\\\n", " "))
    properties = tuple(token for token in tokens if token.startswith("--property="))

    assert tokens[:9] == [
        "/usr/bin/systemd-run",
        "--system",
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
        "--service-type=exec",
        "--unit=$probe_unit",
        "--property=User=root",
    ]
    assert properties == (
        "--property=User=root",
        "--property=Group=root",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=CAP_SYS_PTRACE",
        "--property=AmbientCapabilities=",
        "--property=Environment=LANG=C",
        "--property=Environment=LC_ALL=C",
        "--property=Environment=PATH=/usr/bin:/bin",
        "--property=UnsetEnvironment=LD_PRELOAD",
        "--property=UnsetEnvironment=LD_LIBRARY_PATH",
        "--property=UnsetEnvironment=LD_AUDIT",
        "--property=UnsetEnvironment=LD_DEBUG",
        "--property=UnsetEnvironment=BASH_ENV",
        "--property=UnsetEnvironment=ENV",
        "--property=UnsetEnvironment=GCONV_PATH",
        "--property=PrivateNetwork=yes",
        "--property=IPAddressDeny=any",
        "--property=PrivateDevices=yes",
        "--property=DevicePolicy=closed",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=ReadOnlyPaths=/",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectClock=yes",
        "--property=ProtectHostname=yes",
        "--property=ProtectProc=default",
        "--property=ProcSubset=all",
        "--property=RestrictSUIDSGID=yes",
        "--property=RestrictRealtime=yes",
        "--property=RestrictNamespaces=yes",
        "--property=LockPersonality=yes",
        "--property=MemoryDenyWriteExecute=yes",
        "--property=RemoveIPC=yes",
        "--property=KeyringMode=private",
        "--property=SystemCallArchitectures=native",
        "--property=SystemCallFilter=@system-service",
        (
            "--property=SystemCallFilter=~@network-io @mount @reboot @swap "
            "@obsolete @raw-io @resources @cpu-emulation @debug ptrace "
            "process_vm_readv process_vm_writev pidfd_getfd kcmp"
        ),
        "--property=SystemCallErrorNumber=EPERM",
        "--property=RuntimeMaxSec=5s",
        "--property=TimeoutStartSec=5s",
        "--property=TimeoutStopSec=1s",
        "--property=Restart=no",
        "--property=UMask=0077",
    )
    separator = tokens.index("--")
    assert tokens[separator + 1 :] == [
        "$setpriv_binary",
        "--no-new-privs",
        "--",
        "$repair_probe",
        "probe-helper",
        "$old_helper_pid",
        "$old_helper_start",
        "$probe_nonce",
        "$authority_sha256",
        "$authority_installed_helper_sha256",
        "$old_boot_id",
        "$old_helper_invocation",
        "$authority_setpriv_sha256",
        "$repair_probe_sha256",
    ]

    agent_start = runner.index("agent_probe_output=$(/usr/bin/systemd-run")
    agent_end = runner.index(") || return 1", agent_start)
    agent_command = runner[agent_start + len("agent_probe_output=$(") : agent_end]
    agent_tokens = shlex.split(agent_command.replace("\\\n", " "))
    agent_properties = tuple(
        token for token in agent_tokens if token.startswith("--property=")
    )

    assert agent_tokens[:10] == [
        "/usr/bin/systemd-run",
        "--system",
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
        "--service-type=exec",
        "--unit=$agent_probe_unit",
        "--property=User=vonk-agent",
        "--property=Group=vonk-agent",
    ]
    assert agent_properties == (
        "--property=User=vonk-agent",
        "--property=Group=vonk-agent",
        "--property=SupplementaryGroups=",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=",
        "--property=AmbientCapabilities=",
        "--property=Environment=LANG=C",
        "--property=Environment=LC_ALL=C",
        "--property=Environment=PATH=/usr/bin:/bin",
        "--property=UnsetEnvironment=LD_PRELOAD",
        "--property=UnsetEnvironment=LD_LIBRARY_PATH",
        "--property=UnsetEnvironment=LD_AUDIT",
        "--property=UnsetEnvironment=LD_DEBUG",
        "--property=UnsetEnvironment=BASH_ENV",
        "--property=UnsetEnvironment=ENV",
        "--property=UnsetEnvironment=GCONV_PATH",
        "--property=PrivateNetwork=yes",
        "--property=IPAddressDeny=any",
        "--property=PrivateDevices=yes",
        "--property=DevicePolicy=closed",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=ReadOnlyPaths=/",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectClock=yes",
        "--property=ProtectHostname=yes",
        "--property=ProtectProc=default",
        "--property=ProcSubset=all",
        "--property=RestrictSUIDSGID=yes",
        "--property=RestrictRealtime=yes",
        "--property=RestrictNamespaces=yes",
        "--property=LockPersonality=yes",
        "--property=MemoryDenyWriteExecute=yes",
        "--property=RemoveIPC=yes",
        "--property=KeyringMode=private",
        "--property=SystemCallArchitectures=native",
        "--property=SystemCallFilter=@system-service",
        (
            "--property=SystemCallFilter=~@network-io @mount @reboot @swap "
            "@obsolete @raw-io @resources @cpu-emulation @debug ptrace "
            "process_vm_readv process_vm_writev pidfd_getfd kcmp"
        ),
        "--property=SystemCallErrorNumber=EPERM",
        "--property=RuntimeMaxSec=5s",
        "--property=TimeoutStartSec=5s",
        "--property=TimeoutStopSec=1s",
        "--property=Restart=no",
        "--property=UMask=0077",
    )
    agent_separator = agent_tokens.index("--")
    assert agent_tokens[agent_separator + 1 :] == [
        "$repair_probe",
        "probe-agent",
        "$old_agent_pid",
        "$old_agent_start",
        "$probe_nonce",
        "$authority_sha256",
        "$authority_installed_agent_sha256",
        "$old_boot_id",
        "$old_agent_invocation",
        "$vonk_agent_uid",
        "$vonk_agent_gid",
        "$old_agent_groups",
        "$authority_setpriv_sha256",
        "$repair_probe_sha256",
    ]


def test_repair_hidden_handoff_rejects_every_partial_repair_artifact() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    absent = runner[
        runner.index("public_repair_entries_absent() {") : runner.index(
            "clean_repair_temps() {"
        )
    ]
    dispatcher = runner[runner.index("# VONK_REPAIR_DISPATCH_V1") :]

    for evidence in (
        '"$repair_state"',
        '"$repair_ready"',
        '"$repair_retired"',
        '"$repair_receipt"',
        '"$repair_helper_receipt"',
        '"$source_state"/.repair-build.*',
    ):
        assert evidence in absent
    assert '[ ! -e "$path" ] && [ ! -L "$path" ]' in absent
    assert '[ ! -e "$build_tree" ] && [ ! -L "$build_tree" ]' in absent
    assert 'exec "$standard_runner"' not in dispatcher

    # Build-time placeholders are deliberately reserved for capsule binding.
    # Runtime normalization sentinels must not share that syntax or the
    # fail-closed builder will correctly reject an otherwise complete package.
    assert "Status: VONK_NORMALIZED_STATUS" in runner
    assert "Status: @STATUS@" not in runner

    arm = runner[
        runner.index("arm_repair() {") : runner.index("load_active_repair() {")
    ]
    assert (
        arm.index("cleanup_incomplete_build_trees")
        < arm.index("\n    repair_entries_absent")
        < arm.index("stage_standard_runner")
    )


def test_repair_dpkg_state_parser_executes_fail_closed_with_system_awk(
    tmp_path: Path,
) -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    parser = runner[
        runner.index("dpkg_database_package_state() {") : runner.index(
            "dpkg_transition_record() {"
        )
    ]
    status_path = tmp_path / "status"
    exact = (
        "Package: vonk-forge-agent\n"
        "Status: install ok installed\n"
        "Architecture: arm64\n"
        "Version: 0.1.0~dev.335+g2eaaf4d9b2b5\n\n"
    )
    cases = (
        ("required", exact, True),
        ("optional", exact, True),
        ("optional", "Package: unrelated\nStatus: install ok installed\n\n", True),
        ("required", "Package: unrelated\nStatus: install ok installed\n\n", False),
        ("optional", "Package:\tvonk-forge-agent\n" + exact.split("\n", 1)[1], False),
        ("optional", exact.replace("Package:", "package:", 1), False),
        (
            "required",
            exact.replace("install ok installed", "install ok unpacked"),
            False,
        ),
        (
            "optional",
            exact.replace("install ok installed", "install ok unpacked"),
            False,
        ),
        (
            "required",
            exact.replace("Architecture: arm64", "Architecture: amd64"),
            False,
        ),
        (
            "optional",
            exact.replace("Architecture: arm64", "Architecture: amd64"),
            False,
        ),
        ("required", exact.replace("dev.335", "dev.334"), False),
        ("optional", exact.replace("dev.335", "dev.334"), False),
        (
            "required",
            exact.replace("Package:", "Package: unrelated\nPackage:", 1),
            False,
        ),
        (
            "optional",
            exact.replace("Package:", "Package: unrelated\nPackage:", 1),
            False,
        ),
        (
            "required",
            exact.replace("Status:", "Status: install ok installed\nStatus:", 1),
            False,
        ),
        (
            "optional",
            exact.replace("Status:", "Status: install ok installed\nStatus:", 1),
            False,
        ),
        (
            "required",
            exact.replace("Status:", "status: install ok installed\nStatus:", 1),
            False,
        ),
        (
            "required",
            exact.replace("Architecture:", "Architecture: arm64\nArchitecture:", 1),
            False,
        ),
        ("required", exact.replace("Version:", "Version: 0\nVersion:", 1), False),
        ("required", exact + exact, False),
        ("invalid-mode", exact, False),
    )

    for index, (mode, document, expected) in enumerate(cases):
        status_path.write_text(document, encoding="utf-8")
        script = (
            "package_name=vonk-forge-agent\n"
            "authority_source_architecture=arm64\n"
            "authority_installed_version=0.1.0~dev.335+g2eaaf4d9b2b5\n"
            f"{parser}\n"
            "dpkg_database_package_state "
            f"{shlex.quote(str(status_path))} {shlex.quote(mode)}\n"
        )
        result = subprocess.run(
            ["/bin/sh"], input=script, text=True, capture_output=True, check=False
        )
        assert (result.returncode == 0) is expected, (index, result.stderr)


def test_native_repair_failure_dumps_bounded_dpkg_transition_evidence() -> None:
    harness = (ROOT / "tests/nodes/test_agent_upgrade_repair_systemd.sh").read_text()
    diagnostics = harness[
        harness.index("dump_diagnostics() {") : harness.index("cleanup() {")
    ]

    assert "--- dpkg transition journal ---" in diagnostics
    assert "/var/lib/dpkg/status /var/lib/dpkg/status-old" in diagnostics
    assert 'Package: " package' in diagnostics
    assert "sed -n '1,120p'" in diagnostics
    assert "cat /var/lib/dpkg/status" not in diagnostics


def test_repair_hidden_handoff_is_complete_before_unified_runner_swap() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    arm = runner[
        runner.index("arm_repair() {") : runner.index("load_active_repair() {")
    ]
    recover = runner[
        runner.index("recover_repair() {") : runner.index("# VONK_REPAIR_DISPATCH_V1")
    ]

    assert (
        arm.index("stage_standard_runner")
        < arm.index('mktemp -d "$source_state/.repair-build.')
        < arm.index('repair_tree_exact "$repair_build"')
        < arm.index('/usr/bin/sync -f "$source_state"')
        < arm.index('repair_tree_exact_read_only "$repair_build"')
        < arm.index('atomic_install "$0" "$source_active_runner"')
        < arm.rindex("promote_hidden_repair")
    )
    assert (
        recover.index("flock -n -x 9")
        < recover.index("promote_hidden_repair")
        < recover.index("load_active_repair")
    )


def test_exact_a122_runner_ignores_prepared_repair_objects() -> None:
    _require_git_object(f"{REPAIR_BINARY_REVISION}:packaging/debian/preinst")
    old_runner = BUILD_MODULE.source_at_revision(
        REPAIR_BINARY_REVISION, "packaging/debian/preinst"
    ).decode("utf-8")

    for repair_only_path in (
        ".repair-build.",
        ".standard",
        "package-repair",
        "/repair",
    ):
        assert repair_only_path not in old_runner
    assert 'find "$state_dir"' not in old_runner
    assert 'for path in "$state_dir"/' not in old_runner


def test_repair_phase_replay_refreshes_boot_bound_process_receipts() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    refresh = runner[
        runner.index("refresh_target_helper_after_boot() {") : runner.index(
            "write_final_receipt() {"
        )
    ]
    recover = runner[
        runner.index("recover_repair() {") : runner.index("# VONK_REPAIR_DISPATCH_V1")
    ]

    assert (
        refresh.index("helper_receipt_contract_safe")
        < refresh.index("helper_receipt_and_live_safe")
        < refresh.index('restart "$helper_unit"')
        < refresh.index("write_helper_receipt")
    )
    assert recover.count("refresh_target_helper_after_boot") == 2
    assert 'if ! prove_running_unit "$agent_unit"' in recover
    assert 'restart "$agent_unit"' in recover
    assert 'if [ "$phase_name" != agent-proven ]' not in recover


def test_repair_builder_reconstructs_exact_a122_standard_runner() -> None:
    _require_git_object(
        f"{REPAIR_BINARY_REVISION}:packaging/systemd/"
        "vonk-forge-package-upgrade-recover.service"
    )
    authority = {
        "source_target_version": REPAIR_SOURCE_VERSION,
        "source_architecture": "arm64",
        "source_agent_sha256": (
            "f103bd5adb535eb14e71c9553221b228b900dc2715e0c5b989d335791b7ae415"
        ),
        "source_helper_sha256": (
            "53dd86b81d3737ba6e8b00a7c06bca7c490002e7651a73d2cf6ec55daaa60f9e"
        ),
        "source_unit_sha256": (
            "2d3d6eecf8d7d3a74cc65d8c67b6ca15870c0573c2e99b2d7274a41d8cf93ab7"
        ),
        "source_agent_gate_sha256": (
            "ee18122eb4f13003762f6f5b46d9b17d282e44f3d1e8e6f3cb36743251ad6307"
        ),
        "source_runner_sha256": (
            "4584e84c0df3a4a21b54dd02e80f6ab2fb285d7dc74f3968a4e95a95eda7b0ba"
        ),
    }

    runner = BUILD_MODULE.repair_standard_runner(REPAIR_BINARY_REVISION, authority)

    assert hashlib.sha256(runner).hexdigest() == authority["source_runner_sha256"]
    assert re.search(rb"@[A-Z0-9_]+@", runner) is None


def test_repair_builder_and_verifier_reconstruct_capsule_bound_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    source_unit = b"standard recovery unit\n"
    source_gate = b"standard agent gate\n"
    capsule_unit = b"durable capsule unit\n"
    capsule_gate = b"durable capsule agent gate\n"
    capsule_suppression = b"durable single-owner suppression\n"
    source_preinst = b"\n".join(
        (
            b"version=@VERSION@",
            b"architecture=@ARCHITECTURE@",
            b"agent=@AGENT_SHA256@",
            b"helper=@HELPER_SHA256@",
            b"unit_sha=@RECOVERY_UNIT_SHA256@",
            b"unit=@RECOVERY_UNIT_BASE64@",
            b"agent_gate_sha=@AGENT_GATE_SHA256@",
            b"agent_gate=@AGENT_GATE_BASE64@",
            b"capsule_unit_sha=@RECOVERY_CAPSULE_UNIT_SHA256@",
            b"capsule_unit=@RECOVERY_CAPSULE_UNIT_BASE64@",
            b"capsule_gate_sha=@RECOVERY_CAPSULE_GATE_SHA256@",
            b"capsule_gate=@RECOVERY_CAPSULE_GATE_BASE64@",
            b"capsule_suppression_sha=@RECOVERY_CAPSULE_SUPPRESSION_SHA256@",
            b"capsule_suppression=@RECOVERY_CAPSULE_SUPPRESSION_BASE64@",
            b"",
        )
    )
    sources = {
        "packaging/debian/preinst": source_preinst,
        "packaging/systemd/vonk-forge-package-upgrade-recover.service": source_unit,
        (
            "packaging/systemd/vonk-forge-agent.service.d/"
            "20-package-upgrade-recovery.conf"
        ): source_gate,
        (
            "packaging/systemd/vonk-forge-package-upgrade-recover-capsule.service"
        ): capsule_unit,
        (
            "packaging/systemd/vonk-forge-agent.service.d/"
            "10-package-upgrade-capsule.conf"
        ): capsule_gate,
        (
            "packaging/systemd/vonk-forge-package-upgrade-recover.service.d/"
            "10-capsule-owner.conf"
        ): capsule_suppression,
    }

    def source_lookup(selected_revision: str, relative: str) -> bytes:
        assert selected_revision == revision
        return sources[relative]

    monkeypatch.setattr(BUILD_MODULE, "source_at_revision", source_lookup)
    monkeypatch.setattr(VERIFY_MODULE, "_git_source", source_lookup)
    authority = {
        "source_target_version": "0.1.0~dev.500+g0123456789ab",
        "source_architecture": "arm64",
        "source_agent_sha256": "1" * 64,
        "source_helper_sha256": "2" * 64,
        "source_unit_sha256": hashlib.sha256(source_unit).hexdigest(),
        "source_agent_gate_sha256": hashlib.sha256(source_gate).hexdigest(),
        "source_runner_sha256": "0" * 64,
    }
    rendered = VERIFY_MODULE._render_standard_recovery_runner(revision, authority)
    authority["source_runner_sha256"] = hashlib.sha256(rendered).hexdigest()

    assert BUILD_MODULE.repair_standard_runner(revision, authority) == rendered
    assert (
        VERIFY_MODULE._render_standard_recovery_runner(revision, authority) == rendered
    )
    assert re.search(rb"@[A-Z0-9_]+@", rendered) is None


def test_repair_runtime_orders_helper_proof_before_agent_release_and_cleanup() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    recover = runner[runner.index("recover_repair() {") :]

    helper_receipt = recover.index("write_helper_receipt")
    helper_phase = recover.index("write_phase helper-proven")
    repair_unblock = recover.index('remove_exact_file "$repair_blocker"')
    source_unblock = recover.index('remove_exact_file "$source_blocker"')
    agent_restart = recover.index('restart "$agent_unit"')
    agent_proof = recover.index(
        'prove_running_unit "$agent_unit" "$agent_binary" "$target_agent_sha256"'
    )
    final_receipt = recover.index("write_final_receipt")
    final_retirement = recover.index("retire_source_state")

    assert (
        helper_receipt
        < helper_phase
        < repair_unblock
        < source_unblock
        < agent_restart
        < agent_proof
        < final_receipt
        < final_retirement
    )


def test_repair_runtime_binds_recursive_preinst_and_postinst_to_same_contract() -> None:
    runner = (ROOT / "packaging/debian/preinst-repair").read_text()
    postinst = (ROOT / "packaging/debian/postinst-repair").read_text()
    bindings = (
        "authority_sha256",
        "source_intent_sha256",
        "candidate_sha256",
        "candidate_bytes",
        "target_version",
        "architecture",
        "agent_sha256",
        "helper_sha256",
        "repair_runner_sha256",
        "repair_nonce",
    )

    for binding in bindings:
        assert binding in runner
        assert binding in postinst
    for script in (runner, postinst):
        assert "/usr/bin/dpkg" in script
        assert "--install" in script
        assert "--force-confold" in script
        assert "candidate.deb" in script
        assert "0::/system.slice/$" in script
