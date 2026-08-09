from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts/dev-compose-secrets.py"


@pytest.fixture
def secrets_helper() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "vonk_dev_compose_secrets_test",
        HELPER,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _secret_directory(tmp_path: Path) -> tuple[Path, int]:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    descriptor = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    return directory, descriptor


def test_signing_key_staging_rejects_replacement_between_mkdir_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secrets_helper: ModuleType,
) -> None:
    directory, descriptor = _secret_directory(tmp_path)
    temporary = ".git-signing-key." + "a" * 32
    renamed = directory / "renamed-original"
    replacement = directory / temporary
    sentinel = replacement / "attacker-owned"
    real_open = os.open
    keygen_called = False
    swapped = False

    def race_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == temporary and dir_fd == descriptor and not swapped:
            (directory / temporary).rename(renamed)
            replacement.mkdir(mode=0o700)
            sentinel.write_text("preserve\n", encoding="utf-8")
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def record_keygen(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal keygen_called
        keygen_called = True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(secrets_helper.secrets, "token_hex", lambda _size: "a" * 32)
    monkeypatch.setattr(secrets_helper.os, "open", race_open)
    monkeypatch.setattr(secrets_helper.subprocess, "run", record_keygen)
    try:
        with pytest.raises(secrets_helper.SecretPreparationError):
            secrets_helper._generate_signing_key(descriptor)
    finally:
        os.close(descriptor)

    assert swapped
    assert keygen_called is False
    assert renamed.is_dir()
    assert replacement.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_signing_key_cleanup_preserves_replacement_of_validated_staging_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secrets_helper: ModuleType,
) -> None:
    directory, descriptor = _secret_directory(tmp_path)
    temporary = ".git-signing-key." + "b" * 32
    staging = directory / temporary
    renamed = directory / "renamed-original"
    replacement_descriptor = -1
    replacement_identity: tuple[int, int] | None = None

    def generate_then_swap(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal replacement_descriptor, replacement_identity
        key_path = Path(command[-1])
        key_path.write_bytes(b"private\n")
        key_path.chmod(0o600)
        public_path = Path(str(key_path) + ".pub")
        public_path.write_bytes(b"public\n")
        public_path.chmod(0o600)
        staging.rename(renamed)
        staging.mkdir(mode=0o700)
        replacement_descriptor = os.open(
            staging,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        metadata = os.fstat(replacement_descriptor)
        replacement_identity = (metadata.st_dev, metadata.st_ino)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(secrets_helper.secrets, "token_hex", lambda _size: "b" * 32)
    monkeypatch.setattr(secrets_helper.subprocess, "run", generate_then_swap)
    try:
        secrets_helper._generate_signing_key(descriptor)
    finally:
        os.close(descriptor)

    try:
        assert replacement_descriptor >= 0
        assert replacement_identity is not None
        current = staging.stat()
        assert (current.st_dev, current.st_ino) == replacement_identity
        assert list(staging.iterdir()) == []
        assert renamed.is_dir()
        assert list(renamed.iterdir()) == []
        assert (directory / "git-signing-key").is_file()
        assert (directory / "git-signing-key.pub").is_file()
    finally:
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)
