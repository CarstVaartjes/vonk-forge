from __future__ import annotations

import base64
import datetime as dt
import fcntl
import importlib.util
import json
import os
import queue
import re
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "control" / "src"))

from vonk_control.passwords import PasswordVerification, verify_password

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev-runtime-secrets.py"

ENROLL_HOSTNAME = "enroll.example.test"
AGENT_HOSTNAME = "agents.example.test"
REGISTRY_HOSTNAME = "registry.example.test"
MANAGEMENT_CIDRS = "192.0.2.0/24,2001:db8::/64"

DEPLOYMENT_SECRET_NAMES = {
    "admin-password-verifier",
    "agent-ca-certificate",
    "agent-ca-key",
    "agent-proxy-auth",
    "controller-ca",
    "controller-server-certificate",
    "controller-server-key",
    "database-url",
    "git-signing-key",
    "host-runtime-grant-private-key",
    "litellm-master-key",
    "litellm-upstream-key",
    "management-cidrs",
    "postgres-password",
    "token-signing-key",
    "tailscale-oauth-client-id",
    "tailscale-oauth-client-secret",
}
LOCAL_SOURCE_SECRET_NAMES = DEPLOYMENT_SECRET_NAMES | {
    "admin-password",
    "controller-ca-key",
    "git-signing-key.pub",
    "host-runtime-grant-public-key",
}

OAUTH_CLIENT_ID = b"synthetic-tailscale-client-id\n"
OAUTH_CLIENT_SECRET = b"synthetic-tailscale-client-secret\n"
ROTATED_OAUTH_CLIENT_ID = b"rotated-tailscale-client-id\n"
ROTATED_OAUTH_CLIENT_SECRET = b"rotated-tailscale-client-secret\n"
BROWSER_ACCESS_SECRET_NAMES = {
    "admin-password",
    "admin-password-verifier",
    "tailscale-oauth-client-id",
    "tailscale-oauth-client-secret",
}
ROTATION_PREFIX = ".admin-password-rotation-"
ROTATION_PASSWORD = "admin-password"
ROTATION_VERIFIER = "admin-password-verifier"
ROTATION_JOURNAL = "journal"
ROTATION_MANIFEST_TEMPORARY = ".journal.tmp"
OAUTH_ROTATION_PREFIX = ".tailscale-oauth-rotation-"
OAUTH_ROTATION_ID = "2fdb4cf7-f240-4a6f-b52f-06fdb4058d50"
OTHER_OAUTH_ROTATION_ID = "70d4d13a-5575-43d8-ae60-bf42499901c4"
THIRD_OAUTH_ROTATION_ID = "b2569b53-f109-49c7-a197-d37f93e84dd4"
SECOND_ROTATED_OAUTH_CLIENT_ID = b"second-rotated-tailscale-client-id\n"
SECOND_ROTATED_OAUTH_CLIENT_SECRET = b"second-rotated-tailscale-client-secret\n"


def _rotation_child(transaction: Path, kind: str) -> Path:
    child = transaction / kind
    assert child.exists()
    return child


def _write_oauth_inputs(root: Path) -> tuple[Path, Path]:
    root.mkdir(mode=0o700)
    client_id = root / "client-id"
    client_secret = root / "client-secret"
    client_id.write_bytes(OAUTH_CLIENT_ID)
    client_secret.write_bytes(OAUTH_CLIENT_SECRET)
    client_id.chmod(0o600)
    client_secret.chmod(0o600)
    return client_id, client_secret


def _write_oauth_values(
    root: Path, client_id_value: bytes, client_secret_value: bytes
) -> tuple[Path, Path]:
    client_id, client_secret = _write_oauth_inputs(root)
    client_id.write_bytes(client_id_value)
    client_secret.write_bytes(client_secret_value)
    return client_id, client_secret


def _run_generator(
    secrets_dir: Path,
    *extra: str,
    oauth_files: tuple[Path, Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    expanded_extra = list(extra)
    if (
        "--rotate-tailscale-oauth" in expanded_extra
        and "--tailscale-oauth-rotation-id" not in expanded_extra
    ):
        expanded_extra.extend(
            ("--tailscale-oauth-rotation-id", OAUTH_ROTATION_ID)
        )
    with tempfile.TemporaryDirectory(prefix="vonk-oauth-test-") as temporary:
        temporary_root = Path(temporary)
        temporary_root.chmod(0o700)
        client_id, client_secret = oauth_files or _write_oauth_inputs(
            temporary_root / "inputs"
        )
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
                "--tailscale-oauth-client-id-file",
                str(client_id),
                "--tailscale-oauth-client-secret-file",
                str(client_secret),
                *expanded_extra,
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


def _secret_state(
    secrets_dir: Path, names: set[str] | frozenset[str]
) -> dict[str, tuple[bytes, int, int, int, int, int, int]]:
    state: dict[str, tuple[bytes, int, int, int, int, int, int]] = {}
    for name in names:
        path = secrets_dir / name
        metadata = path.stat()
        state[name] = (
            path.read_bytes(),
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            metadata.st_gid,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_nlink,
        )
    return state


def _descriptor_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _prior_browser_access_generation(secrets_dir: Path) -> set[str]:
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    for name in BROWSER_ACCESS_SECRET_NAMES:
        (secrets_dir / name).unlink()
    names = {path.name for path in secrets_dir.iterdir()}
    assert names == LOCAL_SOURCE_SECRET_NAMES - BROWSER_ACCESS_SECRET_NAMES
    return names


def test_existing_directory_descriptor_closes_when_post_open_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    (protected_parent / "runtime").mkdir(mode=0o700)
    runtime = _load_module()
    parent = os.open(protected_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    real_open = runtime.os.open
    real_fstat = runtime.os.fstat
    opened_descriptor: int | None = None

    def record_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal opened_descriptor
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "runtime" and dir_fd == parent:
            opened_descriptor = descriptor
        return descriptor

    def fail_after_open(descriptor: int) -> os.stat_result:
        if descriptor == opened_descriptor:
            raise OSError("synthetic post-open fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(runtime.os, "open", record_open)
    monkeypatch.setattr(runtime.os, "fstat", fail_after_open)
    try:
        with pytest.raises(OSError, match="synthetic post-open fstat failure"):
            runtime._open_existing_directory(parent, "runtime")
    finally:
        runtime.os.close(parent)

    assert opened_descriptor is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        real_fstat(opened_descriptor)


def _prepare_arguments(
    oauth_files: tuple[Path, Path], **extra: object
) -> dict[str, object]:
    client_id, client_secret = oauth_files
    arguments = {
        "management_cidrs": MANAGEMENT_CIDRS,
        "enroll_hostname": ENROLL_HOSTNAME,
        "agent_hostname": AGENT_HOSTNAME,
        "registry_hostname": REGISTRY_HOSTNAME,
        "tailscale_oauth_client_id_file": client_id,
        "tailscale_oauth_client_secret_file": client_secret,
        **extra,
    }
    if arguments.get("rotate_tailscale_oauth") and not arguments.get(
        "tailscale_oauth_rotation_id"
    ):
        arguments["tailscale_oauth_rotation_id"] = OAUTH_ROTATION_ID
    return arguments


class SyntheticRotationCrash(BaseException):
    pass


class RotationFaults:
    def __init__(
        self,
        runtime: ModuleType,
        secrets_dir: Path,
        *,
        old_password: bytes,
        old_verifier: bytes,
    ) -> None:
        self.runtime = runtime
        self.secrets_dir = secrets_dir
        self.old_password = old_password
        self.old_verifier = old_verifier
        self.event = ""
        self.error = False
        self.triggered = False
        self._real = {
            name: getattr(runtime.os, name)
            for name in (
                "close",
                "fsync",
                "listdir",
                "mkdir",
                "open",
                "readlink",
                "replace",
                "rmdir",
                "unlink",
                "write",
            )
        }

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        event: str,
        *,
        error: bool = False,
    ) -> None:
        self.event = event
        self.error = error
        self.triggered = False

        def mkdir(
            path: str,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            self._real["mkdir"](path, mode, dir_fd=dir_fd)
            if str(path).startswith(ROTATION_PREFIX):
                self._trigger("transaction-create")

        def open_file(
            path: str,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            descriptor = self._real["open"](path, flags, mode, dir_fd=dir_fd)
            kind = self._file_kind(descriptor)
            if flags & os.O_EXCL and kind is not None:
                try:
                    self._trigger(f"{kind}-create")
                except BaseException:
                    self._real["close"](descriptor)
                    raise
            return descriptor

        def write(descriptor: int, content: bytes | memoryview) -> int:
            kind = self._file_kind(descriptor)
            if self.event == "manifest-partial-write" and kind == "manifest":
                written = self._real["write"](descriptor, bytes(content)[:7])
                self.triggered = True
                raise OSError("synthetic partial manifest write")
            written = self._real["write"](descriptor, content)
            if kind is not None:
                self._trigger(f"{kind}-write")
            return written

        def fsync(descriptor: int) -> None:
            event_name = self._fsync_event(descriptor)
            if event_name == self.event and self.error:
                self._trigger(event_name)
            self._real["fsync"](descriptor)
            if event_name is not None:
                self._trigger(event_name)

        def replace(*args: object, **kwargs: object) -> None:
            self._real["replace"](*args, **kwargs)
            destination = str(args[1])
            if destination == ROTATION_JOURNAL:
                self._trigger("manifest-rename")
            elif destination == ROTATION_PASSWORD:
                self._trigger("password-rename")
            elif destination == ROTATION_VERIFIER:
                self._trigger("verifier-rename")

        def unlink(path: str, *args: object, **kwargs: object) -> None:
            self._real["unlink"](path, *args, **kwargs)
            if str(path) in {ROTATION_JOURNAL, ".admin-password-rotation"}:
                self._trigger("journal-unlink")

        def rmdir(path: str, *args: object, **kwargs: object) -> None:
            self._real["rmdir"](path, *args, **kwargs)
            if str(path).startswith(ROTATION_PREFIX):
                self._trigger("transaction-remove")

        monkeypatch.setattr(self.runtime.os, "mkdir", mkdir)
        monkeypatch.setattr(self.runtime.os, "open", open_file)
        monkeypatch.setattr(self.runtime.os, "write", write)
        monkeypatch.setattr(self.runtime.os, "fsync", fsync)
        monkeypatch.setattr(self.runtime.os, "replace", replace)
        monkeypatch.setattr(self.runtime.os, "unlink", unlink)
        monkeypatch.setattr(self.runtime.os, "rmdir", rmdir)

    def restore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, function in self._real.items():
            monkeypatch.setattr(self.runtime.os, name, function)

    def _trigger(self, event: str | None) -> None:
        if self.triggered or event != self.event:
            return
        self.triggered = True
        if self.error:
            raise OSError(f"synthetic rotation failure at {event}")
        raise SyntheticRotationCrash(event)

    def _descriptor_path(self, descriptor: int) -> Path:
        return Path(self._real["readlink"](f"/proc/self/fd/{descriptor}"))

    def _file_kind(self, descriptor: int) -> str | None:
        path = self._descriptor_path(descriptor)
        legacy = {
            ".admin-password.rotate": "password",
            ".admin-password-verifier.rotate": "verifier",
            ".admin-password-rotation": "journal",
        }
        if path.name in legacy:
            return legacy[path.name]
        if not path.parent.name.startswith(ROTATION_PREFIX):
            return None
        if path.name == ROTATION_VERIFIER:
            return "verifier"
        if path.name == ROTATION_PASSWORD:
            return "password"
        if path.name == ROTATION_MANIFEST_TEMPORARY:
            return "manifest"
        return None

    def _fsync_event(self, descriptor: int) -> str | None:
        path = self._descriptor_path(descriptor)
        kind = self._file_kind(descriptor)
        if kind is not None:
            return f"{kind}-fsync"
        if path == self.secrets_dir:
            transactions = [
                name
                for name in self._real["listdir"](self.secrets_dir)
                if name.startswith(ROTATION_PREFIX)
            ]
            password = (self.secrets_dir / ROTATION_PASSWORD).read_bytes()
            verifier = (self.secrets_dir / ROTATION_VERIFIER).read_bytes()
            if transactions and password == self.old_password:
                transaction = self.secrets_dir / transactions[0]
                if not self._real["listdir"](transaction):
                    return "root-fsync-transaction"
            if (
                transactions
                and password != self.old_password
                and verifier == self.old_verifier
            ):
                return "root-fsync-password"
            if (
                transactions
                and password != self.old_password
                and verifier != self.old_verifier
            ):
                return "root-fsync-verifier"
            if not transactions and password != self.old_password:
                return "root-fsync-remove"
            return None
        if path.name.startswith(ROTATION_PREFIX):
            entries = set(self._real["listdir"](descriptor))
            password = (self.secrets_dir / ROTATION_PASSWORD).read_bytes()
            verifier = (self.secrets_dir / ROTATION_VERIFIER).read_bytes()
            if (
                entries == {ROTATION_JOURNAL, ROTATION_VERIFIER}
                and password != self.old_password
                and verifier == self.old_verifier
            ):
                return "transaction-fsync-password-move"
            if (
                entries == {ROTATION_JOURNAL}
                and password != self.old_password
                and verifier != self.old_verifier
            ):
                return "transaction-fsync-verifier-move"
            if entries == {ROTATION_JOURNAL}:
                return "transaction-fsync-manifest"
            if entries == {ROTATION_JOURNAL, ROTATION_PASSWORD}:
                return "transaction-fsync-password"
            if entries == {
                ROTATION_JOURNAL,
                ROTATION_PASSWORD,
                ROTATION_VERIFIER,
            }:
                return "transaction-fsync-verifier"
            if not entries:
                return "transaction-fsync-cleanup"
        return None


def _rotation_transaction(secrets_dir: Path) -> Path:
    transactions = [
        path for path in secrets_dir.iterdir() if path.name.startswith(ROTATION_PREFIX)
    ]
    assert len(transactions) == 1
    transaction = transactions[0]
    metadata = transaction.lstat()
    assert stat.S_ISDIR(metadata.st_mode)
    assert metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o700
    return transaction


def _assert_recovered_admin_pair(
    secrets_dir: Path,
    *,
    old_password: bytes,
    old_verifier: bytes,
) -> None:
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES
    password = (secrets_dir / ROTATION_PASSWORD).read_bytes()
    verifier = (secrets_dir / ROTATION_VERIFIER).read_bytes()
    password_value = password.decode("ascii").strip()
    verifier_value = verifier.decode("ascii").strip()
    assert verify_password(verifier_value, password_value).valid
    if password == old_password:
        assert verifier == old_verifier
    else:
        assert not verify_password(
            verifier_value, old_password.decode("ascii").strip()
        ).valid


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
    assert len(module.LOCAL_SOURCE_SECRET_NAMES) == 21
    assert len(module.DEPLOYMENT_SECRET_NAMES) == 17
    assert module.LOCAL_SOURCE_SECRET_NAMES - module.DEPLOYMENT_SECRET_NAMES == {
        "admin-password",
        "controller-ca-key",
        "git-signing-key.pub",
        "host-runtime-grant-public-key",
    }


def test_generates_idempotent_local_runtime_secret_store_without_leaking_values(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")

    first = _run_generator(secrets_dir, oauth_files=oauth_files)

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
    host_runtime_key = _private_key(secrets_dir / "host-runtime-grant-private-key")
    assert isinstance(host_runtime_key, Ed25519PrivateKey)
    assert (
        bytes.fromhex(
            (secrets_dir / "host-runtime-grant-public-key")
            .read_text(encoding="ascii")
            .strip()
        )
        == host_runtime_key.public_key().public_bytes_raw()
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
    _assert_urlsafe_random_token(
        (secrets_dir / "token-signing-key").read_bytes(),
        name="token-signing-key",
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
    admin_password = (
        (secrets_dir / "admin-password").read_text(encoding="ascii").strip()
    )
    admin_verifier = (
        (secrets_dir / "admin-password-verifier").read_text(encoding="ascii").strip()
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", admin_password)
    assert admin_verifier.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert verify_password(admin_verifier, admin_password) == PasswordVerification(
        True, False
    )
    assert (secrets_dir / "tailscale-oauth-client-id").read_bytes() == OAUTH_CLIENT_ID
    assert (
        secrets_dir / "tailscale-oauth-client-secret"
    ).read_bytes() == OAUTH_CLIENT_SECRET

    before = {
        name: (
            (secrets_dir / name).read_bytes(),
            (secrets_dir / name).stat().st_mtime_ns,
        )
        for name in LOCAL_SOURCE_SECRET_NAMES
    }
    second = _run_generator(secrets_dir, oauth_files=oauth_files)

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
            "admin-password",
            "admin-password-verifier",
            "controller-ca-key",
            "controller-server-key",
            "database-url",
            "git-signing-key",
            "host-runtime-grant-private-key",
            "litellm-master-key",
            "litellm-upstream-key",
            "postgres-password",
            "tailscale-oauth-client-id",
            "tailscale-oauth-client-secret",
        },
    )


@pytest.mark.parametrize("fault", ["mode", "symlink", "hardlink"])
def test_rejects_unsafe_oauth_input_before_creating_the_secret_bundle(
    tmp_path: Path, fault: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    client_id, client_secret = _write_oauth_inputs(tmp_path / "oauth-inputs")
    if fault == "mode":
        client_secret.chmod(0o640)
    elif fault == "symlink":
        client_secret.unlink()
        client_secret.symlink_to(client_id)
    else:
        os.link(client_secret, tmp_path / "oauth-secret-copy")

    result = _run_generator(secrets_dir, oauth_files=(client_id, client_secret))

    assert result.returncode == 1
    assert result.stdout == ""
    assert not secrets_dir.exists()
    assert OAUTH_CLIENT_ID.decode().strip() not in result.stderr
    assert OAUTH_CLIENT_SECRET.decode().strip() not in result.stderr


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
    assert {path.name for path in secrets_dir.iterdir()} == (LOCAL_SOURCE_SECRET_NAMES)


def test_explicit_upgrade_adds_only_host_runtime_authority_to_legacy_store(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    (secrets_dir / "host-runtime-grant-private-key").unlink()
    (secrets_dir / "host-runtime-grant-public-key").unlink()
    legacy_names = LOCAL_SOURCE_SECRET_NAMES - {
        "host-runtime-grant-private-key",
        "host-runtime-grant-public-key",
    }
    before = {
        name: (
            (secrets_dir / name).read_bytes(),
            (secrets_dir / name).stat().st_mtime_ns,
        )
        for name in legacy_names
    }

    refused = _run_generator(secrets_dir)
    upgraded = _run_generator(secrets_dir, "--upgrade-host-runtime-authority")

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert upgraded.returncode == 0, upgraded.stderr
    assert {
        name: (
            (secrets_dir / name).read_bytes(),
            (secrets_dir / name).stat().st_mtime_ns,
        )
        for name in legacy_names
    } == before
    private = _private_key(secrets_dir / "host-runtime-grant-private-key")
    assert isinstance(private, Ed25519PrivateKey)
    assert (
        bytes.fromhex(
            (secrets_dir / "host-runtime-grant-public-key")
            .read_text(encoding="ascii")
            .strip()
        )
        == private.public_key().public_bytes_raw()
    )
    _assert_no_sensitive_output(
        refused.stdout + refused.stderr + upgraded.stdout + upgraded.stderr,
        secrets_dir,
        {"host-runtime-grant-private-key"},
    )


def test_explicit_upgrade_recovers_public_key_after_private_key_publication(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    retained_private = (secrets_dir / "host-runtime-grant-private-key").read_bytes()
    (secrets_dir / "host-runtime-grant-public-key").unlink()

    upgraded = _run_generator(secrets_dir, "--upgrade-host-runtime-authority")

    assert upgraded.returncode == 0, upgraded.stderr
    assert (
        secrets_dir / "host-runtime-grant-private-key"
    ).read_bytes() == retained_private
    private = _private_key(secrets_dir / "host-runtime-grant-private-key")
    assert isinstance(private, Ed25519PrivateKey)
    assert (
        bytes.fromhex(
            (secrets_dir / "host-runtime-grant-public-key")
            .read_text(encoding="ascii")
            .strip()
        )
        == private.public_key().public_bytes_raw()
    )


def test_explicit_upgrade_refuses_public_only_or_unknown_legacy_state(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    (secrets_dir / "host-runtime-grant-private-key").unlink()
    retained_public = (secrets_dir / "host-runtime-grant-public-key").read_bytes()

    refused = _run_generator(secrets_dir, "--upgrade-host-runtime-authority")

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert not (secrets_dir / "host-runtime-grant-private-key").exists()
    assert (
        secrets_dir / "host-runtime-grant-public-key"
    ).read_bytes() == retained_public

    (secrets_dir / "unknown").write_bytes(b"unknown\n")
    (secrets_dir / "unknown").chmod(0o600)
    unknown = _run_generator(secrets_dir, "--upgrade-host-runtime-authority")
    assert unknown.returncode == 1
    assert unknown.stdout == ""
    assert "unknown entries" in unknown.stderr


def test_upgrade_browser_access_is_add_only_for_the_exact_prior_generation(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    prior_names = _prior_browser_access_generation(secrets_dir)
    before = _secret_state(secrets_dir, prior_names)
    oauth_files = _write_oauth_inputs(tmp_path / "upgrade-oauth-inputs")

    refused = _run_generator(secrets_dir, oauth_files=oauth_files)
    upgraded = _run_generator(
        secrets_dir, "--upgrade-browser-access", oauth_files=oauth_files
    )

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert upgraded.returncode == 0, upgraded.stderr
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES
    assert _secret_state(secrets_dir, prior_names) == before
    assert (secrets_dir / "tailscale-oauth-client-id").read_bytes() == OAUTH_CLIENT_ID
    assert (
        secrets_dir / "tailscale-oauth-client-secret"
    ).read_bytes() == OAUTH_CLIENT_SECRET
    _assert_no_sensitive_output(
        refused.stdout + refused.stderr + upgraded.stdout + upgraded.stderr,
        secrets_dir,
        BROWSER_ACCESS_SECRET_NAMES,
    )


@pytest.mark.parametrize("state", ["partial", "unknown", "complete"])
def test_upgrade_browser_access_refuses_every_non_prior_generation(
    tmp_path: Path, state: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    prior_names = _prior_browser_access_generation(secrets_dir)
    if state == "partial":
        partial = secrets_dir / "admin-password"
        partial.write_bytes(b"synthetic-partial-admin-password\n")
        partial.chmod(0o600)
    elif state == "unknown":
        unknown = secrets_dir / "unknown"
        unknown.write_bytes(b"synthetic-unknown-entry\n")
        unknown.chmod(0o600)
    else:
        for name in BROWSER_ACCESS_SECRET_NAMES:
            content = b"synthetic-complete-placeholder\n"
            path = secrets_dir / name
            path.write_bytes(content)
            path.chmod(0o600)
    before_names = {path.name for path in secrets_dir.iterdir()}
    before = _secret_state(secrets_dir, before_names)

    result = _run_generator(secrets_dir, "--upgrade-browser-access")

    assert result.returncode == 1
    assert result.stdout == ""
    assert {path.name for path in secrets_dir.iterdir()} == before_names
    assert _secret_state(secrets_dir, before_names) == before
    assert prior_names <= before_names


def test_upgrade_browser_access_removes_only_files_created_by_a_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    prior_names = _prior_browser_access_generation(secrets_dir)
    before = _secret_state(secrets_dir, prior_names)
    oauth_files = _write_oauth_inputs(tmp_path / "upgrade-oauth-inputs")
    runtime = _load_module()
    real_write = runtime._create_owned_file
    writes = 0

    def fail_third_write(directory: int, name: str, content: bytes) -> object:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise runtime.RuntimeSecretError("synthetic upgrade interruption")
        return real_write(directory, name, content)

    monkeypatch.setattr(runtime, "_create_owned_file", fail_third_write)

    with pytest.raises(runtime.RuntimeSecretError, match="synthetic upgrade"):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, upgrade_browser_access=True),
        )

    assert {path.name for path in secrets_dir.iterdir()} == prior_names
    assert _secret_state(secrets_dir, prior_names) == before


def test_upgrade_browser_access_preserves_an_exclusive_create_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    prior_names = _prior_browser_access_generation(secrets_dir)
    before = _secret_state(secrets_dir, prior_names)
    oauth_files = _write_oauth_inputs(tmp_path / "upgrade-oauth-inputs")
    runtime = _load_module()
    before_descriptors = _descriptor_count()
    real_open = runtime.os.open
    foreign = b"synthetic-exclusive-create-collision\n"
    collision: tuple[int, int] | None = None

    def collide_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal collision
        if (
            collision is None
            and path == "admin-password"
            and flags & os.O_EXCL
            and dir_fd is not None
        ):
            descriptor = real_open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            runtime.os.write(descriptor, foreign)
            runtime.os.fchmod(descriptor, 0o600)
            runtime.os.fsync(descriptor)
            metadata = runtime.os.fstat(descriptor)
            collision = (metadata.st_dev, metadata.st_ino)
            runtime.os.close(descriptor)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime.os, "open", collide_open)

    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, upgrade_browser_access=True),
        )

    assert collision is not None
    collision_path = secrets_dir / "admin-password"
    metadata = collision_path.stat()
    assert (metadata.st_dev, metadata.st_ino) == collision
    assert collision_path.read_bytes() == foreign
    assert _secret_state(secrets_dir, prior_names) == before
    assert _descriptor_count() == before_descriptors


def test_upgrade_browser_access_preserves_a_concurrent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    prior_names = _prior_browser_access_generation(secrets_dir)
    before = _secret_state(secrets_dir, prior_names)
    oauth_files = _write_oauth_inputs(tmp_path / "upgrade-oauth-inputs")
    runtime = _load_module()
    before_descriptors = _descriptor_count()
    real_open = runtime.os.open
    foreign = b"synthetic-concurrent-replacement\n"
    replacement: tuple[int, int] | None = None

    def replace_before_second_create(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replacement
        if (
            replacement is None
            and path == "admin-password-verifier"
            and flags & os.O_EXCL
            and dir_fd is not None
        ):
            runtime.os.unlink("admin-password", dir_fd=dir_fd)
            descriptor = real_open(
                "admin-password",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            runtime.os.write(descriptor, foreign)
            runtime.os.fchmod(descriptor, 0o600)
            runtime.os.fsync(descriptor)
            metadata = runtime.os.fstat(descriptor)
            replacement = (metadata.st_dev, metadata.st_ino)
            runtime.os.close(descriptor)
            raise OSError("synthetic concurrent replacement")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime.os, "open", replace_before_second_create)

    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, upgrade_browser_access=True),
        )

    assert replacement is not None
    replacement_path = secrets_dir / "admin-password"
    metadata = replacement_path.stat()
    assert (metadata.st_dev, metadata.st_ino) == replacement
    assert replacement_path.read_bytes() == foreign
    assert _secret_state(secrets_dir, prior_names) == before
    assert _descriptor_count() == before_descriptors


@pytest.mark.parametrize("operation", ["upgrade", "rotation"])
def test_mutation_closes_retained_ownership_descriptors_after_success(
    tmp_path: Path, operation: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    if operation == "upgrade":
        _prior_browser_access_generation(secrets_dir)
        flags = {"upgrade_browser_access": True}
    else:
        created = _run_generator(secrets_dir, oauth_files=oauth_files)
        assert created.returncode == 0, created.stderr
        flags = {"rotate_admin_password": True}
    runtime = _load_module()
    before_descriptors = _descriptor_count()

    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(oauth_files, **flags),
    )

    assert _descriptor_count() == before_descriptors


@pytest.mark.parametrize("operation", ["upgrade", "rotation"])
def test_mutation_closes_retained_ownership_descriptors_after_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    if operation == "upgrade":
        _prior_browser_access_generation(secrets_dir)
        failure_name = "admin-password-verifier"
        flags = {"upgrade_browser_access": True}
    else:
        created = _run_generator(secrets_dir, oauth_files=oauth_files)
        assert created.returncode == 0, created.stderr
        failure_name = ROTATION_PASSWORD
        flags = {"rotate_admin_password": True}
    runtime = _load_module()
    real_create = runtime._create_owned_file
    before_descriptors = _descriptor_count()

    def fail_after_an_owned_entry(directory: int, name: str, content: bytes) -> object:
        if name == failure_name:
            raise runtime.RuntimeSecretError("synthetic retained descriptor failure")
        return real_create(directory, name, content)

    monkeypatch.setattr(runtime, "_create_owned_file", fail_after_an_owned_entry)

    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, **flags),
        )

    assert _descriptor_count() == before_descriptors
    if operation == "rotation":
        monkeypatch.setattr(runtime, "_create_owned_file", real_create)
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files),
        )
        assert _descriptor_count() == before_descriptors


def test_rotation_retains_transaction_descriptor_through_recovery_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    runtime = _load_module()
    real_resume = runtime._resume_admin_rotation
    before_descriptors = _descriptor_count()
    retained_at_handoff: list[bool] = []

    def observe_handoff(directory: int, name: str) -> None:
        transaction = runtime.os.stat(name, dir_fd=directory, follow_symlinks=False)
        retained = False
        for descriptor_path in Path("/proc/self/fd").iterdir():
            try:
                opened = runtime.os.fstat(int(descriptor_path.name))
            except (OSError, ValueError):
                continue
            if (opened.st_dev, opened.st_ino) == (
                transaction.st_dev,
                transaction.st_ino,
            ):
                retained = True
                break
        retained_at_handoff.append(retained)
        real_resume(directory, name)

    monkeypatch.setattr(runtime, "_resume_admin_rotation", observe_handoff)

    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(oauth_files, rotate_admin_password=True),
    )

    assert retained_at_handoff == [True]
    assert _descriptor_count() == before_descriptors


def test_rotation_preserves_and_rejects_an_exclusive_transaction_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    runtime = _load_module()
    token = "a" * 32
    transaction_name = ROTATION_PREFIX + token
    transaction_path = secrets_dir / transaction_name
    foreign = b"synthetic-foreign-transaction-entry\n"
    real_mkdir = runtime.os.mkdir
    real_open = runtime.os.open
    collision: tuple[int, int] | None = None

    def collide_mkdir(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal collision
        if path == transaction_name and collision is None and dir_fd is not None:
            real_mkdir(path, 0o700, dir_fd=dir_fd)
            metadata = runtime.os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            collision = (metadata.st_dev, metadata.st_ino)
            transaction = real_open(
                path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd
            )
            descriptor = real_open(
                "foreign",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=transaction,
            )
            runtime.os.write(descriptor, foreign)
            runtime.os.fchmod(descriptor, 0o600)
            runtime.os.fsync(descriptor)
            runtime.os.close(descriptor)
            runtime.os.close(transaction)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime.secrets, "token_hex", lambda _size: token)
    monkeypatch.setattr(runtime.os, "mkdir", collide_mkdir)

    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, rotate_admin_password=True),
        )

    assert collision is not None
    metadata = transaction_path.stat()
    assert (metadata.st_dev, metadata.st_ino) == collision
    assert (transaction_path / "foreign").read_bytes() == foreign
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before

    monkeypatch.setattr(runtime.os, "mkdir", real_mkdir)
    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files),
        )
    assert (transaction_path / "foreign").read_bytes() == foreign


def test_rotation_preserves_and_rejects_a_concurrent_child_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    runtime = _load_module()
    real_open = runtime.os.open
    foreign = b"synthetic-foreign-rotation-child\n"
    collision: tuple[int, int] | None = None

    def collide_child_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal collision
        parent = ""
        if dir_fd is not None:
            parent = Path(runtime.os.readlink(f"/proc/self/fd/{dir_fd}")).name
        if (
            collision is None
            and parent.startswith(ROTATION_PREFIX)
            and path.startswith(ROTATION_PASSWORD)
            and flags & os.O_EXCL
            and dir_fd is not None
        ):
            descriptor = real_open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            runtime.os.write(descriptor, foreign)
            runtime.os.fchmod(descriptor, 0o600)
            runtime.os.fsync(descriptor)
            metadata = runtime.os.fstat(descriptor)
            collision = (metadata.st_dev, metadata.st_ino)
            runtime.os.close(descriptor)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime.os, "open", collide_child_open)

    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, rotate_admin_password=True),
        )

    transaction_path = _rotation_transaction(secrets_dir)
    child = next(
        path
        for path in transaction_path.iterdir()
        if path.name.startswith(ROTATION_PASSWORD)
    )
    assert collision is not None
    metadata = child.stat()
    assert (metadata.st_dev, metadata.st_ino) == collision
    assert child.read_bytes() == foreign
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before

    monkeypatch.setattr(runtime.os, "open", real_open)
    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files),
        )
    assert child.read_bytes() == foreign


@pytest.mark.parametrize("manifest", [None, b"", b'{"old_password_sha256"'])
def test_rotation_recovers_only_exact_safe_pre_manifest_debris(
    tmp_path: Path, manifest: bytes | None
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    transaction = secrets_dir / (ROTATION_PREFIX + "b" * 32)
    transaction.mkdir(mode=0o700)
    if manifest is not None:
        temporary = transaction / ROTATION_MANIFEST_TEMPORARY
        temporary.write_bytes(manifest)
        temporary.chmod(0o600)

    recovered = _run_generator(secrets_dir, oauth_files=oauth_files)

    assert recovered.returncode == 0, recovered.stderr
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before
    assert not transaction.exists()


@pytest.mark.parametrize(
    "foreign_state",
    ["transaction-mode", "manifest-mode", "manifest-hardlink", "foreign-entry"],
)
def test_rotation_recovery_rejects_foreign_pre_manifest_state(
    tmp_path: Path, foreign_state: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    transaction = secrets_dir / (ROTATION_PREFIX + "c" * 32)
    transaction.mkdir(mode=0o700)
    temporary = transaction / ROTATION_MANIFEST_TEMPORARY
    if foreign_state == "transaction-mode":
        transaction.chmod(0o750)
    elif foreign_state == "manifest-mode":
        temporary.write_bytes(b"synthetic-partial-manifest")
        temporary.chmod(0o640)
    elif foreign_state == "manifest-hardlink":
        temporary.write_bytes(b"synthetic-partial-manifest")
        temporary.chmod(0o600)
        os.link(temporary, transaction / "second-link")
    else:
        foreign = transaction / "foreign"
        foreign.write_bytes(b"synthetic-foreign-state")
        foreign.chmod(0o600)

    refused = _run_generator(secrets_dir, oauth_files=oauth_files)

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert transaction.exists()
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before

    transaction.chmod(0o700)
    for child in transaction.iterdir():
        child.unlink()
    transaction.rmdir()


def test_upgrade_and_rotation_invocations_serialize_on_one_kernel_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    runtime = _load_module()
    real_rotate = runtime._rotate_admin_password
    real_flock = fcntl.flock
    first_inside = threading.Event()
    release_first = threading.Event()
    observation: queue.Queue[str] = queue.Queue()
    guard = threading.Lock()
    lock_attempts = 0
    mutations = 0
    results: list[Exception | None] = []

    def observed_flock(descriptor: int, operation: int) -> None:
        nonlocal lock_attempts
        with guard:
            lock_attempts += 1
            attempt = lock_attempts
        if attempt == 2:
            observation.put("lock-attempt")
        real_flock(descriptor, operation)

    def held_rotation(*args: object, **kwargs: object) -> None:
        nonlocal mutations
        with guard:
            mutations += 1
            invocation = mutations
        if invocation == 1:
            first_inside.set()
            assert release_first.wait(5)
        else:
            observation.put("mutator-entered")
        real_rotate(*args, **kwargs)

    def invoke(**flags: bool) -> None:
        try:
            runtime.prepare_runtime_secrets(
                secrets_dir,
                **_prepare_arguments(oauth_files, **flags),
            )
        except Exception as error:  # noqa: BLE001 - return thread failures to pytest.
            results.append(error)
        else:
            results.append(None)

    monkeypatch.setattr(
        runtime,
        "fcntl",
        SimpleNamespace(flock=observed_flock, LOCK_EX=fcntl.LOCK_EX),
        raising=False,
    )
    monkeypatch.setattr(runtime, "_rotate_admin_password", held_rotation)
    first = threading.Thread(
        target=invoke, kwargs={"rotate_admin_password": True}, daemon=True
    )
    second = threading.Thread(
        target=invoke, kwargs={"rotate_admin_password": True}, daemon=True
    )
    first.start()
    assert first_inside.wait(5)
    management_cidrs = secrets_dir / "management-cidrs"
    original_lock_inode = management_cidrs.stat().st_ino
    replacement = secrets_dir / ".synthetic-management-cidrs-replacement"
    replacement.write_bytes(management_cidrs.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(management_cidrs)
    assert management_cidrs.stat().st_ino != original_lock_inode
    second.start()

    assert observation.get(timeout=5) == "lock-attempt"
    with pytest.raises(queue.Empty):
        observation.get(timeout=0.5)
    assert mutations == 1
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results == [None, None]
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES


def test_rotation_fsyncs_both_directories_after_each_payload_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    runtime = _load_module()
    real_replace = runtime.os.replace
    real_fsync = runtime.os.fsync
    real_readlink = runtime.os.readlink
    events: list[str] = []

    def replace(*args: object, **kwargs: object) -> None:
        real_replace(*args, **kwargs)
        destination = str(args[1])
        if destination in {ROTATION_PASSWORD, ROTATION_VERIFIER}:
            events.append(f"move-{destination}")

    def fsync(descriptor: int) -> None:
        path = Path(real_readlink(f"/proc/self/fd/{descriptor}"))
        if path == secrets_dir:
            events.append("fsync-root")
        elif path.parent == secrets_dir and path.name.startswith(ROTATION_PREFIX):
            events.append("fsync-transaction")
        real_fsync(descriptor)

    monkeypatch.setattr(runtime.os, "replace", replace)
    monkeypatch.setattr(runtime.os, "fsync", fsync)

    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(oauth_files, rotate_admin_password=True),
    )

    password_move = events.index(f"move-{ROTATION_PASSWORD}")
    verifier_move = events.index(f"move-{ROTATION_VERIFIER}")
    assert events[password_move : password_move + 3] == [
        f"move-{ROTATION_PASSWORD}",
        "fsync-root",
        "fsync-transaction",
    ]
    assert events[verifier_move : verifier_move + 3] == [
        f"move-{ROTATION_VERIFIER}",
        "fsync-root",
        "fsync-transaction",
    ]


@pytest.mark.parametrize("persisted_move", ["password", "verifier"])
def test_rotation_replays_payload_when_destination_persists_before_source_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persisted_move: str,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    oauth_files = _write_oauth_inputs(tmp_path / "oauth-inputs")
    created = _run_generator(secrets_dir, oauth_files=oauth_files)
    assert created.returncode == 0, created.stderr
    old_password = (secrets_dir / ROTATION_PASSWORD).read_bytes()
    old_verifier = (secrets_dir / ROTATION_VERIFIER).read_bytes()
    runtime = _load_module()
    faults = RotationFaults(
        runtime,
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )
    faults.install(monkeypatch, "transaction-fsync-verifier")
    with pytest.raises(SyntheticRotationCrash):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, rotate_admin_password=True),
        )
    faults.restore(monkeypatch)
    transaction = _rotation_transaction(secrets_dir)
    new_password = (transaction / ROTATION_PASSWORD).read_bytes()
    new_verifier = (transaction / ROTATION_VERIFIER).read_bytes()

    def persist_destination(name: str, content: bytes) -> None:
        temporary = secrets_dir / f".synthetic-persisted-{name}"
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        temporary.replace(secrets_dir / name)

    persist_destination(ROTATION_PASSWORD, new_password)
    if persisted_move == "verifier":
        (transaction / ROTATION_PASSWORD).unlink()
        persist_destination(ROTATION_VERIFIER, new_verifier)

    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(oauth_files),
    )

    assert (secrets_dir / ROTATION_PASSWORD).read_bytes() == new_password
    assert (secrets_dir / ROTATION_VERIFIER).read_bytes() == new_verifier
    assert not transaction.exists()


def test_rotate_admin_password_changes_only_the_credential_pair_when_explicit(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    old_password = before["admin-password"][0].decode("ascii").strip()
    old_verifier = before["admin-password-verifier"][0].decode("ascii").strip()

    unchanged = _run_generator(secrets_dir)
    assert unchanged.returncode == 0, unchanged.stderr
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before

    rotated = _run_generator(secrets_dir, "--rotate-admin-password")

    assert rotated.returncode == 0, rotated.stderr
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES
    after = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    assert {name for name in after if after[name] != before[name]} == {
        "admin-password",
        "admin-password-verifier",
    }
    new_password = after["admin-password"][0].decode("ascii").strip()
    new_verifier = after["admin-password-verifier"][0].decode("ascii").strip()
    assert new_password != old_password
    assert new_verifier != old_verifier
    assert verify_password(new_verifier, new_password).valid
    assert not verify_password(new_verifier, old_password).valid
    _assert_no_sensitive_output(
        unchanged.stdout + unchanged.stderr + rotated.stdout + rotated.stderr,
        secrets_dir,
        BROWSER_ACCESS_SECRET_NAMES,
    )
    assert old_password not in rotated.stdout + rotated.stderr
    assert old_verifier not in rotated.stdout + rotated.stderr


def test_rotate_tailscale_oauth_changes_only_the_oauth_pair_when_explicit(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )

    rotated = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=replacement,
    )

    assert rotated.returncode == 0, rotated.stderr
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES
    after = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    assert {name for name in after if after[name] != before[name]} == {
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
    }
    assert after["tailscale-oauth-client-id"][0] == ROTATED_OAUTH_CLIENT_ID
    assert (
        after["tailscale-oauth-client-secret"][0]
        == ROTATED_OAUTH_CLIENT_SECRET
    )
    for name in LOCAL_SOURCE_SECRET_NAMES - {
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
    }:
        assert after[name] == before[name]
    assert ROTATED_OAUTH_CLIENT_ID.decode().strip() not in (
        rotated.stdout + rotated.stderr
    )
    assert ROTATED_OAUTH_CLIENT_SECRET.decode().strip() not in (
        rotated.stdout + rotated.stderr
    )
    receipt = next(protected_parent.glob(".vonk-*.tailscale-oauth-rotation-receipt"))
    _assert_regular_private_file(receipt)
    receipt_document = json.loads(receipt.read_text(encoding="ascii"))
    assert receipt_document["schema_version"] == 1
    assert receipt_document["operations"][0]["rotation_id"] == OAUTH_ROTATION_ID
    assert ROTATED_OAUTH_CLIENT_ID.strip() not in receipt.read_bytes()
    assert ROTATED_OAUTH_CLIENT_SECRET.strip() not in receipt.read_bytes()

    exact_retry = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=replacement,
    )
    assert exact_retry.returncode == 0, exact_retry.stderr

    different_operation = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        "--tailscale-oauth-rotation-id",
        OTHER_OAUTH_ROTATION_ID,
        oauth_files=replacement,
    )
    assert different_operation.returncode == 1
    assert "credentials were previously used" in different_operation.stderr


def test_rotate_tailscale_oauth_rejects_aliased_inputs_without_mutation(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )

    refused = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=(replacement[0], replacement[0]),
    )

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before
    assert ROTATED_OAUTH_CLIENT_ID.decode().strip() not in refused.stderr


def test_rotate_tailscale_oauth_rejects_unchanged_credentials_without_mutation(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)

    refused = _run_generator(secrets_dir, "--rotate-tailscale-oauth")

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before
    assert OAUTH_CLIENT_ID.decode().strip() not in refused.stderr
    assert OAUTH_CLIENT_SECRET.decode().strip() not in refused.stderr


@pytest.mark.parametrize(
    "fault", ["empty", "mode", "symlink", "directory", "hardlink"]
)
def test_rotate_tailscale_oauth_rejects_unsafe_inputs_without_mutation(
    tmp_path: Path, fault: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    client_id, client_secret = replacement
    if fault == "empty":
        client_secret.write_bytes(b"")
    elif fault == "mode":
        client_secret.chmod(0o640)
    elif fault == "symlink":
        client_secret.unlink()
        client_secret.symlink_to(client_id)
    elif fault == "directory":
        client_secret.unlink()
        client_secret.mkdir(mode=0o700)
    else:
        client_secret.unlink()
        os.link(client_id, client_secret)

    refused = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=replacement,
    )

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before
    assert ROTATED_OAUTH_CLIENT_ID.decode().strip() not in refused.stderr
    assert ROTATED_OAUTH_CLIENT_SECRET.decode().strip() not in refused.stderr


@pytest.mark.parametrize(
    ("option", "input_name"),
    (
        ("--tailscale-oauth-client-id-file", "client-id"),
        ("--tailscale-oauth-client-secret-file", "client-secret"),
    ),
)
def test_rotate_tailscale_oauth_rejects_a_partial_input_pair(
    tmp_path: Path, option: str, input_name: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    replacement_root = tmp_path / "replacement-oauth-inputs"
    replacement = _write_oauth_values(
        replacement_root,
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    input_path = replacement[0] if input_name == "client-id" else replacement[1]

    refused = subprocess.run(
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
            option,
            str(input_path),
            "--rotate-tailscale-oauth",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert refused.returncode == 2
    assert refused.stdout == ""
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before
    assert ROTATED_OAUTH_CLIENT_ID.decode().strip() not in refused.stderr
    assert ROTATED_OAUTH_CLIENT_SECRET.decode().strip() not in refused.stderr


def test_rotate_tailscale_oauth_rolls_back_an_injected_second_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    runtime = _load_module()
    real_replace = runtime.os.replace

    def fail_second_install(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if (
            source == "new-client-secret"
            and destination == "tailscale-oauth-client-secret"
        ):
            raise OSError("synthetic OAuth install failure")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(runtime.os, "replace", fail_second_install)

    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(replacement, rotate_tailscale_oauth=True),
        )

    after = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    for name in LOCAL_SOURCE_SECRET_NAMES - {
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
    }:
        assert after[name] == before[name]
    assert after["tailscale-oauth-client-id"][0] == before[
        "tailscale-oauth-client-id"
    ][0]
    assert after["tailscale-oauth-client-secret"][0] == before[
        "tailscale-oauth-client-secret"
    ][0]
    _assert_regular_private_file(secrets_dir / "tailscale-oauth-client-id")
    _assert_regular_private_file(secrets_dir / "tailscale-oauth-client-secret")
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES


def test_rotate_tailscale_oauth_recovers_a_crash_by_restoring_the_old_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = {
        name: (secrets_dir / name).read_bytes() for name in LOCAL_SOURCE_SECRET_NAMES
    }
    original_inputs = _write_oauth_values(
        tmp_path / "original-oauth-inputs",
        OAUTH_CLIENT_ID,
        OAUTH_CLIENT_SECRET,
    )
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    runtime = _load_module()
    real_replace = runtime.os.replace

    def crash_during_second_install(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if (
            source == "new-client-secret"
            and destination == "tailscale-oauth-client-secret"
        ):
            raise SyntheticRotationCrash
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(runtime.os, "replace", crash_during_second_install)
    with pytest.raises(SyntheticRotationCrash):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(replacement, rotate_tailscale_oauth=True),
        )
    assert any(
        path.name.startswith(".tailscale-oauth-rotation-")
        for path in secrets_dir.iterdir()
    )

    monkeypatch.setattr(runtime.os, "replace", real_replace)
    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(original_inputs),
    )

    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES
    assert {
        name: (secrets_dir / name).read_bytes() for name in LOCAL_SOURCE_SECRET_NAMES
    } == before


@pytest.mark.parametrize(
    "boundary",
    [
        "destination-fsync",
        "commit-marker-fsync",
        "backup-cleanup",
        "journal-cleanup",
        "committed-cleanup",
    ],
)
def test_rotate_tailscale_oauth_exact_retry_recovers_every_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    runtime = _load_module()
    real_fsync = runtime.os.fsync
    real_unlink = runtime.os.unlink
    triggered = False

    def descriptor_path(descriptor: int) -> Path:
        return Path(os.readlink(f"/proc/self/fd/{descriptor}"))

    def crash_at_fsync(descriptor: int) -> None:
        nonlocal triggered
        real_fsync(descriptor)
        if triggered:
            return
        path = descriptor_path(descriptor)
        transactions = [
            candidate
            for candidate in secrets_dir.iterdir()
            if candidate.name.startswith(OAUTH_ROTATION_PREFIX)
        ]
        if not transactions:
            return
        entries = {candidate.name for candidate in transactions[0].iterdir()}
        pair_is_new = (
            (secrets_dir / "tailscale-oauth-client-id").read_bytes()
            == ROTATED_OAUTH_CLIENT_ID
            and (secrets_dir / "tailscale-oauth-client-secret").read_bytes()
            == ROTATED_OAUTH_CLIENT_SECRET
        )
        if (
            boundary == "destination-fsync"
            and path == secrets_dir
            and pair_is_new
            and "committed" not in entries
        ) or (
            boundary == "commit-marker-fsync"
            and path == transactions[0]
            and "committed" in entries
        ):
            triggered = True
            raise SyntheticRotationCrash(boundary)

    def crash_during_cleanup(
        path: str, *args: object, **kwargs: object
    ) -> None:
        nonlocal triggered
        real_unlink(path, *args, **kwargs)
        if (
            boundary == "backup-cleanup" and path == "old-client-id"
        ) or (
            boundary == "journal-cleanup" and path == ROTATION_JOURNAL
        ) or (
            boundary == "committed-cleanup" and path == "committed"
        ):
            triggered = True
            raise SyntheticRotationCrash(boundary)

    monkeypatch.setattr(runtime.os, "fsync", crash_at_fsync)
    monkeypatch.setattr(runtime.os, "unlink", crash_during_cleanup)
    with pytest.raises(SyntheticRotationCrash):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(replacement, rotate_tailscale_oauth=True),
        )
    assert triggered

    monkeypatch.setattr(runtime.os, "fsync", real_fsync)
    monkeypatch.setattr(runtime.os, "unlink", real_unlink)
    recovered = runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(replacement, rotate_tailscale_oauth=True),
    )

    assert recovered == secrets_dir
    assert (secrets_dir / "tailscale-oauth-client-id").read_bytes() == (
        ROTATED_OAUTH_CLIENT_ID
    )
    assert (secrets_dir / "tailscale-oauth-client-secret").read_bytes() == (
        ROTATED_OAUTH_CLIENT_SECRET
    )
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES


@pytest.mark.parametrize("cleanup_name", [ROTATION_JOURNAL, "committed"])
def test_rotate_tailscale_oauth_post_commit_cleanup_error_returns_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_name: str,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    runtime = _load_module()
    real_unlink = runtime.os.unlink
    triggered = False

    def fail_once_after_journal_cleanup(
        path: str, *args: object, **kwargs: object
    ) -> None:
        nonlocal triggered
        real_unlink(path, *args, **kwargs)
        if not triggered and path == cleanup_name:
            triggered = True
            raise OSError("synthetic post-commit cleanup failure")

    monkeypatch.setattr(runtime.os, "unlink", fail_once_after_journal_cleanup)
    result = runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(replacement, rotate_tailscale_oauth=True),
    )

    assert triggered
    assert result == secrets_dir
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES
    assert (secrets_dir / "tailscale-oauth-client-id").read_bytes() == (
        ROTATED_OAUTH_CLIENT_ID
    )


def test_forged_committed_state_cannot_authorize_a_different_rotation_id(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    runtime = _load_module()
    transaction = secrets_dir / (OAUTH_ROTATION_PREFIX + "d" * 32)
    transaction.mkdir(mode=0o700)
    committed = transaction / "committed"
    committed.write_bytes(
        runtime._oauth_rotation_journal(
            OAUTH_CLIENT_ID,
            OAUTH_CLIENT_SECRET,
            OAUTH_CLIENT_ID,
            OAUTH_CLIENT_SECRET,
            OAUTH_ROTATION_ID,
        )
    )
    committed.chmod(0o600)

    refused = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        "--tailscale-oauth-rotation-id",
        OTHER_OAUTH_ROTATION_ID,
    )

    assert refused.returncode == 1
    assert "credentials were previously used" in refused.stderr
    assert OAUTH_CLIENT_ID.decode().strip() not in refused.stderr
    assert OAUTH_CLIENT_SECRET.decode().strip() not in refused.stderr


def test_stale_rotation_id_and_previously_used_pair_cannot_roll_back_credentials(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    first = _write_oauth_values(
        tmp_path / "first-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    second = _write_oauth_values(
        tmp_path / "second-oauth-inputs",
        SECOND_ROTATED_OAUTH_CLIENT_ID,
        SECOND_ROTATED_OAUTH_CLIENT_SECRET,
    )
    first_rotation = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=first,
    )
    assert first_rotation.returncode == 0, first_rotation.stderr
    second_rotation = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        "--tailscale-oauth-rotation-id",
        OTHER_OAUTH_ROTATION_ID,
        oauth_files=second,
    )
    assert second_rotation.returncode == 0, second_rotation.stderr
    current = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)

    stale_retry = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=first,
    )
    reused_pair = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        "--tailscale-oauth-rotation-id",
        THIRD_OAUTH_ROTATION_ID,
        oauth_files=first,
    )

    assert stale_retry.returncode == 1
    assert "rotation ID was already used" in stale_retry.stderr
    assert reused_pair.returncode == 1
    assert "credentials were previously used" in reused_pair.stderr
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == current


def test_long_generation_name_uses_a_bounded_receipt_filename(tmp_path: Path) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / ("r" * 195)
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )

    rotated = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=replacement,
    )

    assert rotated.returncode == 0, rotated.stderr
    receipts = list(
        protected_parent.glob(".vonk-*.tailscale-oauth-rotation-receipt")
    )
    assert len(receipts) == 1
    assert len(os.fsencode(receipts[0].name)) <= 255
    assert {path.name for path in secrets_dir.iterdir()} == LOCAL_SOURCE_SECRET_NAMES


def test_owned_receipt_temporary_is_removed_under_the_generation_lock(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    replacement = _write_oauth_values(
        tmp_path / "replacement-oauth-inputs",
        ROTATED_OAUTH_CLIENT_ID,
        ROTATED_OAUTH_CLIENT_SECRET,
    )
    rotated = _run_generator(
        secrets_dir,
        "--rotate-tailscale-oauth",
        oauth_files=replacement,
    )
    assert rotated.returncode == 0, rotated.stderr
    receipt = next(protected_parent.glob(".vonk-*.tailscale-oauth-rotation-receipt"))
    temporary = receipt.with_name(f"{receipt.name}.tmp")
    temporary.write_bytes(receipt.read_bytes())
    temporary.chmod(0o600)

    recovered = _run_generator(secrets_dir, oauth_files=replacement)

    assert recovered.returncode == 0, recovered.stderr
    assert not temporary.exists()
    assert receipt.exists()


@pytest.mark.parametrize("state", ["prejournal", "postjournal"])
def test_rotate_tailscale_oauth_recovery_rejects_foreign_transaction_state(
    tmp_path: Path, state: str
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    before = _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES)
    transaction = secrets_dir / (OAUTH_ROTATION_PREFIX + "c" * 32)
    transaction.mkdir(mode=0o700)
    if state == "postjournal":
        runtime = _load_module()
        journal = transaction / "journal"
        journal.write_bytes(
            runtime._oauth_rotation_journal(
                OAUTH_CLIENT_ID,
                OAUTH_CLIENT_SECRET,
                ROTATED_OAUTH_CLIENT_ID,
                ROTATED_OAUTH_CLIENT_SECRET,
                OAUTH_ROTATION_ID,
            )
        )
        journal.chmod(0o600)
    foreign = transaction / "old-client-id"
    foreign.write_bytes(b"synthetic-foreign-state\n")
    foreign.chmod(0o600)

    refused = _run_generator(secrets_dir)

    assert refused.returncode == 1
    assert refused.stdout == ""
    assert transaction.exists()
    assert foreign.read_bytes() == b"synthetic-foreign-state\n"
    assert _secret_state(secrets_dir, LOCAL_SOURCE_SECRET_NAMES) == before


@pytest.mark.parametrize(
    "event",
    [
        "transaction-create",
        "root-fsync-transaction",
        "manifest-create",
        "manifest-write",
        "manifest-fsync",
        "manifest-rename",
        "transaction-fsync-manifest",
        "password-create",
        "password-write",
        "password-fsync",
        "transaction-fsync-password",
        "verifier-create",
        "verifier-write",
        "verifier-fsync",
        "transaction-fsync-verifier",
        "password-rename",
        "transaction-fsync-password-move",
        "root-fsync-password",
        "verifier-rename",
        "transaction-fsync-verifier-move",
        "root-fsync-verifier",
        "journal-unlink",
        "transaction-fsync-cleanup",
        "transaction-remove",
        "root-fsync-remove",
    ],
)
def test_rotate_admin_password_recovers_every_persisted_crash_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    oauth_files = _write_oauth_inputs(tmp_path / "rotation-oauth-inputs")
    old_password = (secrets_dir / ROTATION_PASSWORD).read_bytes()
    old_verifier = (secrets_dir / ROTATION_VERIFIER).read_bytes()
    runtime = _load_module()
    faults = RotationFaults(
        runtime,
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )
    faults.install(monkeypatch, event)

    with pytest.raises(SyntheticRotationCrash):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, rotate_admin_password=True),
        )
    assert faults.triggered

    if event == "transaction-fsync-manifest":
        transaction = _rotation_transaction(secrets_dir)
        journal_path = _rotation_child(transaction, ROTATION_JOURNAL)
        _assert_regular_private_file(journal_path)
        journal_raw = journal_path.read_bytes()
        journal = json.loads(journal_raw)
        assert set(journal) == {
            "old_password_sha256",
            "old_verifier_sha256",
            "new_password_sha256",
            "new_verifier_sha256",
        }
        assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in journal.values())
        assert old_password.strip() not in journal_raw
        assert old_verifier.strip() not in journal_raw
        assert len(list(transaction.iterdir())) == 1

    faults.restore(monkeypatch)
    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(oauth_files),
    )

    _assert_recovered_admin_pair(
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )


@pytest.mark.parametrize(
    "event",
    [
        "transaction-create",
        "root-fsync-transaction",
        "manifest-create",
        "manifest-partial-write",
        "manifest-write",
        "manifest-fsync",
        "manifest-rename",
        "transaction-fsync-manifest",
        "password-create",
        "password-write",
        "password-fsync",
        "transaction-fsync-password",
        "verifier-create",
        "verifier-write",
        "verifier-fsync",
        "transaction-fsync-verifier",
        "password-rename",
        "transaction-fsync-password-move",
        "root-fsync-password",
        "verifier-rename",
        "transaction-fsync-verifier-move",
        "root-fsync-verifier",
        "journal-unlink",
        "transaction-fsync-cleanup",
        "transaction-remove",
        "root-fsync-remove",
    ],
)
def test_rotate_admin_password_recovers_every_filesystem_error_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    oauth_files = _write_oauth_inputs(tmp_path / "rotation-oauth-inputs")
    old_password = (secrets_dir / ROTATION_PASSWORD).read_bytes()
    old_verifier = (secrets_dir / ROTATION_VERIFIER).read_bytes()
    runtime = _load_module()
    faults = RotationFaults(
        runtime,
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )
    faults.install(monkeypatch, event, error=True)

    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, rotate_admin_password=True),
        )
    assert faults.triggered

    faults.restore(monkeypatch)
    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(oauth_files),
    )
    _assert_recovered_admin_pair(
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )


@pytest.mark.parametrize(
    "event",
    [
        "password-rename",
        "transaction-fsync-password-move",
        "root-fsync-password",
        "verifier-rename",
        "transaction-fsync-verifier-move",
        "root-fsync-verifier",
        "journal-unlink",
        "transaction-fsync-cleanup",
        "transaction-remove",
        "root-fsync-remove",
    ],
)
def test_admin_rotation_recovery_is_itself_resumable_after_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    secrets_dir = protected_parent / "runtime"
    created = _run_generator(secrets_dir)
    assert created.returncode == 0, created.stderr
    oauth_files = _write_oauth_inputs(tmp_path / "rotation-oauth-inputs")
    old_password = (secrets_dir / ROTATION_PASSWORD).read_bytes()
    old_verifier = (secrets_dir / ROTATION_VERIFIER).read_bytes()
    runtime = _load_module()
    faults = RotationFaults(
        runtime,
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )
    faults.install(monkeypatch, "transaction-fsync-verifier")
    with pytest.raises(SyntheticRotationCrash):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files, rotate_admin_password=True),
        )
    faults.restore(monkeypatch)

    recovery_faults = RotationFaults(
        runtime,
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )
    recovery_faults.install(monkeypatch, event, error=True)
    with pytest.raises(runtime.RuntimeSecretError):
        runtime.prepare_runtime_secrets(
            secrets_dir,
            **_prepare_arguments(oauth_files),
        )
    assert recovery_faults.triggered

    recovery_faults.restore(monkeypatch)
    runtime.prepare_runtime_secrets(
        secrets_dir,
        **_prepare_arguments(oauth_files),
    )
    _assert_recovered_admin_pair(
        secrets_dir,
        old_password=old_password,
        old_verifier=old_verifier,
    )


def test_default_store_is_the_gitignored_local_development_directory(
    tmp_path: Path,
) -> None:
    development = tmp_path / ".dev"
    client_id, client_secret = _write_oauth_inputs(tmp_path / "oauth-inputs")

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
            "--tailscale-oauth-client-id-file",
            str(client_id),
            "--tailscale-oauth-client-secret-file",
            str(client_secret),
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    destination = development / "vonk-forge-secrets"
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == str(destination)
    assert {path.name for path in destination.iterdir()} == (LOCAL_SOURCE_SECRET_NAMES)


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
    client_id, client_secret = _write_oauth_inputs(tmp_path / "oauth-inputs")
    monkeypatch.setattr(secrets, "_filesystem_type", lambda _path: "cifs")

    with pytest.raises(secrets.RuntimeSecretError):
        secrets.prepare_runtime_secrets(
            destination,
            management_cidrs=MANAGEMENT_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
            tailscale_oauth_client_id_file=client_id,
            tailscale_oauth_client_secret_file=client_secret,
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
