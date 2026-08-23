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
    assert "/usr/bin/loginctl disable-linger vonk-agent" in prerm


def test_upgrade_postinst_is_local_and_cannot_poison_dpkg_on_controller_failure() -> (
    None
):
    postinst = POSTINST.read_text()

    assert 'if [ -n "${2:-}" ]' in postinst
    upgrade_guard = postinst.index('if [ -n "${2:-}" ]')
    assert postinst.index("deb-systemd-invoke restart", upgrade_guard) > upgrade_guard
    assert postinst.index("self-test", upgrade_guard) > upgrade_guard
    for forbidden in (
        "verify-readiness",
        "MainPID",
        "is-active",
        "readiness.json",
        "/usr/bin/sleep",
        "curl",
        "wget",
    ):
        assert forbidden not in postinst


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
    assert "new_version='0.1.0'" in preinst
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
    assert (
        "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_FSETID "
        "CAP_NET_ADMIN CAP_SETGID CAP_SETUID"
    ) in helper_unit.splitlines()
    assert "AF_INET" not in helper_unit
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
