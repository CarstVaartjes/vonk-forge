from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


RESOURCE_PACKAGE = "vonk_control.resources.dev"
EXPECTED_RESOURCES = {
    "Caddyfile": 0o444,
    "caddy-entrypoint.sh": 0o555,
    "litellm-bootstrap.json": 0o444,
    "litellm-entrypoint.sh": 0o555,
    "litellm-supervisor.py": 0o555,
}
MAXIMUM_RESOURCE_BYTES = 128 * 1024


def _runtime_assets():
    return importlib.import_module("vonk_control.dev_runtime_assets")


def _litellm_supervisor():
    path = importlib.resources.files(RESOURCE_PACKAGE).joinpath(
        "litellm-supervisor.py"
    )
    spec = importlib.util.spec_from_file_location("test_litellm_supervisor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_development_resources_are_complete_bounded_regular_files() -> None:
    package = importlib.resources.files(RESOURCE_PACKAGE)

    for name in EXPECTED_RESOURCES:
        resource = package.joinpath(name)
        assert resource.is_file(), name
        assert not resource.is_symlink(), name
        content = resource.read_bytes()
        assert 0 < len(content) <= MAXIMUM_RESOURCE_BYTES, name


def test_stage_development_assets_atomically_replaces_complete_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    destination = tmp_path / "runtime-config"
    destination.mkdir()
    for name in EXPECTED_RESOURCES:
        (destination / name).write_bytes(f"old-{name}\n".encode())
    real_replace = os.replace
    replacements: list[str] = []

    def inspect_replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd is not None
        assert dst_dir_fd == src_dir_fd
        target_name = os.fspath(target)
        assert (destination / target_name).read_bytes() == (
            f"old-{target_name}\n".encode()
        )
        source_metadata = os.stat(
            source,
            dir_fd=src_dir_fd,
            follow_symlinks=False,
        )
        assert stat.S_ISREG(source_metadata.st_mode)
        assert os.stat(
            target,
            dir_fd=dst_dir_fd,
            follow_symlinks=False,
        ).st_nlink == 1
        assert Path(f"/proc/self/fd/{src_dir_fd}").resolve() == destination
        source_content = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=src_dir_fd,
        )
        try:
            with os.fdopen(source_content, "rb", closefd=False) as staged:
                assert staged.read() == importlib.resources.files(
                    RESOURCE_PACKAGE
                ).joinpath(target_name).read_bytes()
        finally:
            os.close(source_content)
        replacements.append(target_name)
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(runtime_assets.os, "replace", inspect_replace)

    runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)

    assert set(replacements) == set(EXPECTED_RESOURCES)
    assert {path.name for path in destination.iterdir()} == set(EXPECTED_RESOURCES)
    for name, mode in EXPECTED_RESOURCES.items():
        target = destination / name
        assert target.read_bytes() == importlib.resources.files(
            RESOURCE_PACKAGE
        ).joinpath(name).read_bytes()
        assert stat.S_IMODE(target.stat().st_mode) == mode


def test_stage_development_assets_assigns_exact_service_owners_when_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    ownership: dict[str, tuple[int, int]] = {}

    def record_owner(descriptor: int, uid: int, gid: int) -> None:
        name = Path(os.readlink(f"/proc/self/fd/{descriptor}")).name
        if name.startswith(".") and name.endswith(".new"):
            projected_name = name[1:].split(".", 1)[0]
            ownership[projected_name] = (uid, gid)

    monkeypatch.setattr(runtime_assets.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_assets.os, "fchown", record_owner)

    runtime_assets.stage_development_assets(
        RESOURCE_PACKAGE,
        tmp_path / "runtime-config",
    )

    assert ownership == {
        "Caddyfile": (10000, 10000),
        "caddy-entrypoint": (10000, 10000),
        "litellm-bootstrap": (10002, 10001),
        "litellm-entrypoint": (10002, 10001),
        "litellm-supervisor": (10002, 10001),
    }


def test_stage_development_assets_rejects_a_symlink_target_without_touching_it(
    tmp_path: Path,
) -> None:
    runtime_assets = _runtime_assets()
    destination = tmp_path / "runtime-config"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve\n")
    (destination / "Caddyfile").symlink_to(outside)

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)

    assert (destination / "Caddyfile").is_symlink()
    assert outside.read_bytes() == b"preserve\n"


def test_stage_development_assets_rejects_a_symlink_package_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    package_root = tmp_path / "test_assets"
    package_root.mkdir()
    (package_root / "__init__.py").write_bytes(b"")
    outside = tmp_path / "outside-resource"
    outside.write_bytes(b"do-not-stage\n")
    for name in EXPECTED_RESOURCES:
        target = package_root / name
        if name == "Caddyfile":
            target.symlink_to(outside)
        else:
            target.write_bytes(f"safe-{name}\n".encode())
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(
            "test_assets",
            tmp_path / "runtime-config",
        )

    assert not (tmp_path / "runtime-config" / "Caddyfile").exists()
    assert outside.read_bytes() == b"do-not-stage\n"


def test_litellm_supervisor_materializes_file_secrets_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _litellm_supervisor()
    secret_values = {
        "os.environ/LITELLM_MASTER_KEY": "master-file-sentinel",
        "os.environ/LITELLM_UPSTREAM_KEY": "upstream-file-sentinel",
        "os.environ/LITELLM_DATABASE_URL": (
            "postgresql://control:database-file-sentinel@postgres/control"
        ),
    }
    secret_paths: dict[str, Path] = {}
    for marker, value in secret_values.items():
        path = tmp_path / marker.removeprefix("os.environ/").lower()
        path.write_text(value + "\n", encoding="utf-8")
        secret_paths[marker] = path
        monkeypatch.setenv(marker.removeprefix("os.environ/"), "environment-leak")
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "general_settings": {
                    "database_url": "os.environ/LITELLM_DATABASE_URL",
                    "master_key": "os.environ/LITELLM_MASTER_KEY",
                },
                "model_list": [
                    {
                        "litellm_params": {
                            "api_key": "os.environ/LITELLM_UPSTREAM_KEY"
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "effective.json"
    monkeypatch.setattr(supervisor, "SECRET_FILES", secret_paths)

    effective = supervisor._materialize_config(source, destination=destination)

    assert effective == destination
    document = json.loads(destination.read_bytes())
    assert document["general_settings"] == {
        "database_url": secret_values["os.environ/LITELLM_DATABASE_URL"],
        "master_key": secret_values["os.environ/LITELLM_MASTER_KEY"],
    }
    assert document["model_list"][0]["litellm_params"]["api_key"] == (
        secret_values["os.environ/LITELLM_UPSTREAM_KEY"]
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert all(
        os.environ[name.removeprefix("os.environ/")] == "environment-leak"
        for name in secret_values
    )
