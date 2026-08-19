from __future__ import annotations

import os
from pathlib import Path

import pytest
from vonk_control.runtime_init import (
    RuntimeSecretError,
    install_admin_grant_key,
    stage_private_key,
)


def test_admin_grant_key_can_be_staged_without_host_owner_assumptions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"private-admin-grant-key\n")
    source.chmod(0o660)
    destination = tmp_path / "normalized" / "admin-grant-private-key.pem"

    stage_private_key(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )

    assert destination.read_bytes() == b"private-admin-grant-key\n"
    assert destination.stat().st_mode & 0o777 == 0o444


def test_staged_api_private_key_is_owned_by_the_api_and_private(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"private-api-key\n")
    destination = tmp_path / "normalized" / "api-key"

    stage_private_key(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
        mode=0o400,
    )

    assert destination.stat().st_mode & 0o777 == 0o400


def test_admin_grant_key_is_copied_to_a_private_api_runtime_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"private-admin-grant-key\n")
    source.chmod(0o444)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    destination = install_admin_grant_key(
        source,
        runtime,
        source_uid=os.geteuid(),
        api_uid=os.geteuid(),
        api_gid=os.getegid(),
    )

    assert destination == runtime / "admin-grant-private-key.pem"
    assert destination.read_bytes() == b"private-admin-grant-key\n"
    assert destination.stat().st_mode & 0o777 == 0o400
    assert runtime.stat().st_mode & 0o777 == 0o710


def test_admin_grant_key_rotation_replaces_the_runtime_inode_atomically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"first-private-key\n")
    source.chmod(0o444)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    first = install_admin_grant_key(
        source,
        runtime,
        source_uid=os.geteuid(),
        api_uid=os.geteuid(),
        api_gid=os.getegid(),
    )
    first_inode = first.stat().st_ino
    source.chmod(0o644)
    source.write_bytes(b"second-private-key\n")
    source.chmod(0o444)

    second = install_admin_grant_key(
        source,
        runtime,
        source_uid=os.geteuid(),
        api_uid=os.geteuid(),
        api_gid=os.getegid(),
    )

    assert second.read_bytes() == b"second-private-key\n"
    assert second.stat().st_ino != first_inode
    assert second.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("fault", ("symlink", "hardlink", "writable"))
def test_admin_grant_key_rejects_unsafe_source(tmp_path: Path, fault: str) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"private-admin-grant-key\n")
    source.chmod(0o444)
    if fault == "symlink":
        actual = source
        source = tmp_path / "link.pem"
        source.symlink_to(actual)
    elif fault == "hardlink":
        os.link(source, tmp_path / "second-name.pem")
    else:
        source.chmod(0o644)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    with pytest.raises(RuntimeSecretError):
        install_admin_grant_key(
            source,
            runtime,
            source_uid=os.geteuid(),
            api_uid=os.geteuid(),
            api_gid=os.getegid(),
        )

    assert not (runtime / "admin-grant-private-key.pem").exists()
