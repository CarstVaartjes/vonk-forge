from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev-runtime-secrets.py"

ENROLL_HOSTNAME = "enroll.example.test"
AGENT_HOSTNAME = "agents.example.test"
REGISTRY_HOSTNAME = "registry.example.test"
MANAGEMENT_CIDRS = "192.0.2.0/24,2001:db8::/64"

DEPLOYMENT_SECRET_NAMES = {
    "agent-ca-certificate",
    "agent-ca-key",
    "agent-proxy-auth",
    "controller-ca",
    "controller-server-certificate",
    "controller-server-key",
    "database-url",
    "git-signing-key",
    "litellm-master-key",
    "litellm-upstream-key",
    "management-cidrs",
    "postgres-password",
}
LOCAL_SOURCE_SECRET_NAMES = DEPLOYMENT_SECRET_NAMES | {
    "controller-ca-key",
    "git-signing-key.pub",
}


def _run_generator(secrets_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "--secrets-dir",
            str(secrets_dir),
            "--management-cidrs",
            MANAGEMENT_CIDRS,
            "--enroll-hostname",
            ENROLL_HOSTNAME,
            "--agent-hostname",
            AGENT_HOSTNAME,
            "--registry-hostname",
            REGISTRY_HOSTNAME,
            *extra,
        ),
        check=False,
        capture_output=True,
        text=True,
    )


def _load_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "vonk_dev_runtime_secrets_test",
        SCRIPT,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _private_key(path: Path) -> object:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _assert_regular_private_file(path: Path) -> None:
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode), path.name
    assert metadata.st_uid == os.geteuid(), path.name
    assert metadata.st_nlink == 1, path.name
    assert stat.S_IMODE(metadata.st_mode) == 0o600, path.name
    assert metadata.st_size > 0, path.name


def _assert_urlsafe_random_token(raw: bytes, *, name: str) -> None:
    token = raw.decode("ascii").strip()
    assert raw == (token + "\n").encode("ascii")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token), name
    assert "=" not in token
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert len(decoded) >= 32, name


def _assert_no_sensitive_output(
    combined_output: str, secrets_dir: Path, names: set[str]
) -> None:
    for name in names:
        raw = (secrets_dir / name).read_bytes().strip()
        markers: list[str] = []
        if b"\n" in raw:
            markers.extend(
                line.decode("ascii", errors="ignore")
                for line in raw.splitlines()
                if len(line) >= 24 and b"-----" not in line
            )
        else:
            markers.append(raw.decode("ascii", errors="ignore"))
        for marker in markers:
            if marker and marker in combined_output:
                pytest.fail(f"secret value from {name} leaked to command output")


def test_certificate_window_supports_the_ubuntu_cryptography_api() -> None:
    module = _load_module()
    before = dt.datetime(2026, 1, 1, tzinfo=dt.UTC).replace(tzinfo=None)
    after = dt.datetime(2027, 1, 1, tzinfo=dt.UTC).replace(tzinfo=None)

    class LegacyCertificate:
        not_valid_before = before
        not_valid_after = after

    assert module._certificate_window(LegacyCertificate()) == (
        before.replace(tzinfo=dt.UTC),
        after.replace(tzinfo=dt.UTC),
    )


def test_declares_local_source_and_deployment_secret_boundaries() -> None:
    module = _load_module()

    assert module.LOCAL_SOURCE_SECRET_NAMES == LOCAL_SOURCE_SECRET_NAMES
    assert module.DEPLOYMENT_SECRET_NAMES == DEPLOYMENT_SECRET_NAMES
    assert module.RUNTIME_SECRET_NAMES == LOCAL_SOURCE_SECRET_NAMES
    assert len(module.LOCAL_SOURCE_SECRET_NAMES) == 14
    assert len(module.DEPLOYMENT_SECRET_NAMES) == 12
    assert module.LOCAL_SOURCE_SECRET_NAMES - module.DEPLOYMENT_SECRET_NAMES == {
        "controller-ca-key",
        "git-signing-key.pub",
    }


def test_generates_idempotent_local_runtime_secret_store_without_leaking_values(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"

    first = _run_generator(secrets_dir)

    assert first.returncode == 0, first.stderr
    output = dict(line.split("=", 1) for line in first.stdout.splitlines()[1:])
    assert first.stdout.splitlines()[0] == str(secrets_dir)
    assert set(output) == {
        "agent-ca-not-after",
        "agent-ca-sha256",
        "controller-ca-not-after",
        "controller-ca-sha256",
        "controller-server-not-after",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", output["agent-ca-sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", output["controller-ca-sha256"])
    assert output["agent-ca-not-after"].endswith("Z")
    assert output["controller-ca-not-after"].endswith("Z")
    assert output["controller-server-not-after"].endswith("Z")
    assert first.stderr == ""
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    for name in LOCAL_SOURCE_SECRET_NAMES:
        _assert_regular_private_file(secrets_dir / name)

    agent_ca = _certificate(secrets_dir / "agent-ca-certificate")
    controller_ca = _certificate(secrets_dir / "controller-ca")
    server = _certificate(secrets_dir / "controller-server-certificate")
    assert agent_ca.subject != controller_ca.subject
    assert agent_ca.serial_number != controller_ca.serial_number
    assert isinstance(agent_ca.public_key(), Ed25519PublicKey)
    assert isinstance(_private_key(secrets_dir / "agent-ca-key"), Ed25519PrivateKey)
    controller_key = _private_key(secrets_dir / "controller-ca-key")
    assert isinstance(controller_key, Ed25519PrivateKey)
    assert (
        controller_ca.public_key().public_bytes_raw()
        == controller_key.public_key().public_bytes_raw()
    )
    assert agent_ca.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value == x509.BasicConstraints(ca=True, path_length=0)
    key_usage = agent_ca.extensions.get_extension_for_class(x509.KeyUsage).value
    assert key_usage.key_cert_sign is True
    assert key_usage.crl_sign is True
    assert server.issuer == controller_ca.subject
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert set(san.value.get_values_for_type(x509.DNSName)) == {
        ENROLL_HOSTNAME,
        AGENT_HOSTNAME,
        REGISTRY_HOSTNAME,
    }

    _assert_urlsafe_random_token(
        (secrets_dir / "agent-proxy-auth").read_bytes(),
        name="agent-proxy-auth",
    )
    _assert_urlsafe_random_token(
        (secrets_dir / "litellm-master-key").read_bytes(),
        name="litellm-master-key",
    )
    _assert_urlsafe_random_token(
        (secrets_dir / "litellm-upstream-key").read_bytes(),
        name="litellm-upstream-key",
    )
    postgres_password = (secrets_dir / "postgres-password").read_text(encoding="ascii")
    assert re.fullmatch(r"[0-9a-f]{64}\n", postgres_password)
    assert (secrets_dir / "database-url").read_text(encoding="ascii") == (
        "postgresql+psycopg://control:"
        f"{postgres_password.strip()}@postgres:5432/control\n"
    )
    assert (secrets_dir / "management-cidrs").read_text(encoding="ascii") == (
        "192.0.2.0/24\n2001:db8::/64\n"
    )

    before = {
        name: (
            (secrets_dir / name).read_bytes(),
            (secrets_dir / name).stat().st_mtime_ns,
        )
        for name in LOCAL_SOURCE_SECRET_NAMES
    }
    second = _run_generator(secrets_dir)

    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
    assert second.stderr == ""
    after = {
        name: (
            (secrets_dir / name).read_bytes(),
            (secrets_dir / name).stat().st_mtime_ns,
        )
        for name in before
    }
    assert after == before
    _assert_no_sensitive_output(
        first.stdout + first.stderr + second.stdout + second.stderr,
        secrets_dir,
        {
            "agent-ca-key",
            "agent-proxy-auth",
            "controller-ca-key",
            "controller-server-key",
            "database-url",
            "git-signing-key",
            "litellm-master-key",
            "litellm-upstream-key",
            "postgres-password",
        },
    )


def test_rejects_symlink_secret_directory_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    symlink = tmp_path / "linked-runtime"
    symlink.symlink_to(target, target_is_directory=True)

    result = _run_generator(symlink)

    assert result.returncode == 1
    assert result.stdout == ""
    assert list(target.iterdir()) == []


def test_rejects_symlink_in_secret_directory_ancestry(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    protected = real_parent / "protected"
    protected.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    result = _run_generator(linked_parent / "protected" / "runtime")

    assert result.returncode == 1
    assert result.stdout == ""
    assert not (protected / "runtime").exists()


def test_generates_into_an_existing_empty_private_directory(tmp_path: Path) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    secrets_dir.mkdir(mode=0o700)

    result = _run_generator(secrets_dir)

    assert result.returncode == 0, result.stderr
    assert {path.name for path in secrets_dir.iterdir()} == (
        LOCAL_SOURCE_SECRET_NAMES
    )


def test_default_store_is_the_gitignored_local_development_directory(
    tmp_path: Path,
) -> None:
    development = tmp_path / ".dev"

    result = subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "--management-cidrs",
            MANAGEMENT_CIDRS,
            "--enroll-hostname",
            ENROLL_HOSTNAME,
            "--agent-hostname",
            AGENT_HOSTNAME,
            "--registry-hostname",
            REGISTRY_HOSTNAME,
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    destination = development / "vonk-forge-secrets"
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == str(destination)
    assert {path.name for path in destination.iterdir()} == (
        LOCAL_SOURCE_SECRET_NAMES
    )


def test_rejects_group_writable_secret_parent(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o770)

    result = _run_generator(unsafe_parent / "runtime")

    assert result.returncode == 1
    assert result.stdout == ""
    assert not (unsafe_parent / "runtime").exists()


def test_rejects_hardlinked_existing_secret_before_generating_more(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "runtime"
    secrets_dir.mkdir(mode=0o700)
    password = secrets_dir / "postgres-password"
    password.write_text("0" * 64 + "\n", encoding="ascii")
    password.chmod(0o600)
    os.link(password, tmp_path / "hardlinked-password")

    result = _run_generator(secrets_dir)

    assert result.returncode == 1
    assert result.stdout == ""
    assert sorted(path.name for path in secrets_dir.iterdir()) == ["postgres-password"]


def test_rejects_existing_agent_ca_not_signed_by_its_declared_key(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    original = _certificate(secrets_dir / "agent-ca-certificate")
    agent_key = _private_key(secrets_dir / "agent-ca-key")
    assert isinstance(agent_key, Ed25519PrivateKey)
    attacker_key = Ed25519PrivateKey.generate()
    now = dt.datetime.now(dt.UTC)
    tampered = (
        x509.CertificateBuilder()
        .subject_name(original.subject)
        .issuer_name(original.subject)
        .public_key(agent_key.public_key())
        .serial_number(original.serial_number)
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(agent_key.public_key()),
            critical=False,
        )
        .sign(attacker_key, algorithm=None)
    )
    (secrets_dir / "agent-ca-certificate").write_bytes(
        tampered.public_bytes(serialization.Encoding.PEM)
    )

    reused = _run_generator(secrets_dir)

    assert reused.returncode == 1
    assert reused.stdout == ""
    assert "PRIVATE" not in reused.stderr
    assert original.subject == x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Development Agent CA")]
    )


def test_rejects_existing_controller_ca_not_matching_its_retained_key(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    replacement = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (secrets_dir / "controller-ca-key").write_bytes(replacement)

    reused = _run_generator(secrets_dir)

    assert reused.returncode == 1
    assert reused.stdout == ""
    assert "PRIVATE" not in reused.stderr


def test_rejects_existing_server_certificate_with_extra_key_usage(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    runtime = _load_module()
    controller_key = Ed25519PrivateKey.generate()
    controller_ca = runtime._ca_certificate(
        "Vonk Forge Development Controller CA", controller_key
    )
    server_key = _private_key(secrets_dir / "controller-server-key")
    assert isinstance(server_key, Ed25519PrivateKey)
    now = dt.datetime.now(dt.UTC)
    server = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(
                        NameOID.COMMON_NAME, "Vonk Forge Development Controller"
                    )
                ]
            )
        )
        .issuer_name(controller_ca.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(ENROLL_HOSTNAME),
                    x509.DNSName(AGENT_HOSTNAME),
                    x509.DNSName(REGISTRY_HOSTNAME),
                ]
            ),
            critical=False,
        )
        .sign(controller_key, algorithm=None)
    )
    (secrets_dir / "controller-ca").write_bytes(
        controller_ca.public_bytes(serialization.Encoding.PEM)
    )
    (secrets_dir / "controller-server-certificate").write_bytes(
        server.public_bytes(serialization.Encoding.PEM)
    )

    reused = _run_generator(secrets_dir)

    assert reused.returncode == 1
    assert reused.stdout == ""


def test_rejects_windows_or_smb_filesystem_for_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = _load_module()
    destination = tmp_path / "runtime"
    destination.mkdir(mode=0o700)
    monkeypatch.setattr(secrets, "_filesystem_type", lambda _path: "cifs")

    with pytest.raises(secrets.RuntimeSecretError):
        secrets.prepare_runtime_secrets(
            destination,
            management_cidrs=MANAGEMENT_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    result = _run_generator(Path("C:\\\\vonk-forge\\\\secrets"))

    assert result.returncode == 1
    assert result.stdout == ""


def test_rejects_invalid_hostnames_and_cidrs(tmp_path: Path) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)

    bad_hostname = _run_generator(
        protected_parent / "bad-hostname",
        "--agent-hostname",
        "bad_host.example",
    )
    bad_cidr = _run_generator(
        protected_parent / "bad-cidr",
        "--management-cidrs",
        "192.0.2.12/24",
    )

    assert bad_hostname.returncode == 1
    assert bad_hostname.stdout == ""
    assert bad_cidr.returncode == 1
    assert bad_cidr.stdout == ""
