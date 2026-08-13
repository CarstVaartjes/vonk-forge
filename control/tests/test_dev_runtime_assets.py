from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import importlib.util
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
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
    path = importlib.resources.files(RESOURCE_PACKAGE).joinpath("litellm-supervisor.py")
    spec = importlib.util.spec_from_file_location("test_litellm_supervisor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _litellm_document(*, model_name: str = "chat") -> dict[str, object]:
    models: list[dict[str, object]] = []
    if model_name:
        models.append(
            {
                "litellm_params": {
                    "api_base": "http://10.0.0.2:8000/v1",
                    "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                    "model": f"openai/{model_name}",
                    "rpm": 10,
                    "tpm": 1000,
                },
                "model_name": model_name,
            }
        )
    return {
        "general_settings": {
            "database_url": "os.environ/LITELLM_DATABASE_URL",
            "disable_admin_ui": False,
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "store_model_in_db": False,
        },
        "litellm_settings": {
            "drop_params": True,
            "failure_callback": [],
            "set_verbose": False,
            "success_callback": [],
        },
        "model_list": models,
        "router_settings": {
            "enable_pre_call_checks": True,
            "routing_strategy": "simple-shuffle",
        },
    }


def _canonical_json(document: dict[str, object]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _active_litellm_bundle(
    supervisor: object,
    tmp_path: Path,
    config: bytes,
    *,
    now: datetime,
) -> Path:
    routes = b'{"routes":{"chat":{}},"state":"published"}\n'
    manifest = {
        "evidence_set_digest": "b" * 64,
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
        "generation": 1,
        "issued_at": (now - timedelta(seconds=1)).isoformat(),
        "litellm_sha256": hashlib.sha256(config).hexdigest(),
        "plan_digest": "a" * 64,
        "reconciliation_id": "bb7aac18-edbf-4cc1-bafd-15e282557c53",
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "schema_version": 1,
        "state": "published",
    }
    manifest_content = _canonical_json(manifest)
    manifest_sha = hashlib.sha256(manifest_content).hexdigest()
    directory_name = f"00000001-{manifest_sha}"
    root = tmp_path / "routes"
    generation = root / "generations" / directory_name
    generation.mkdir(parents=True)
    (generation / "manifest.json").write_bytes(manifest_content)
    (generation / "routes.json").write_bytes(routes)
    selected = generation / "litellm.json"
    selected.write_bytes(config)
    activation = {
        **manifest,
        "directory": directory_name,
        "manifest_sha256": manifest_sha,
    }
    (root / "activation.json").write_bytes(_canonical_json(activation))
    supervisor.ROOT = root
    supervisor.ACTIVATION = root / "activation.json"
    supervisor.GENERATIONS = root / "generations"
    return selected


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
        assert (
            os.stat(
                target,
                dir_fd=dst_dir_fd,
                follow_symlinks=False,
            ).st_nlink
            == 1
        )
        assert Path(f"/proc/self/fd/{src_dir_fd}").resolve() == destination
        source_content = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=src_dir_fd,
        )
        try:
            with os.fdopen(source_content, "rb", closefd=False) as staged:
                assert (
                    staged.read()
                    == importlib.resources.files(RESOURCE_PACKAGE)
                    .joinpath(target_name)
                    .read_bytes()
                )
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
        assert (
            target.read_bytes()
            == importlib.resources.files(RESOURCE_PACKAGE).joinpath(name).read_bytes()
        )
        assert stat.S_IMODE(target.stat().st_mode) == mode


def test_stage_development_assets_preserves_unchanged_live_mount_inodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    destination = tmp_path / "runtime-config"
    runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)
    identities = {
        name: (destination / name).stat().st_ino for name in EXPECTED_RESOURCES
    }

    def reject_replace(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unchanged runtime assets must preserve their inode")

    monkeypatch.setattr(runtime_assets.os, "replace", reject_replace)
    runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)

    assert {
        name: (destination / name).stat().st_ino for name in EXPECTED_RESOURCES
    } == identities


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


def test_stage_development_assets_rejects_non_filesystem_resources_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()

    class NonFilesystemResource:
        def joinpath(self, _name: str) -> NonFilesystemResource:
            return self

        def read_bytes(self) -> bytes:
            raise AssertionError("non-filesystem resource must not be read")

    monkeypatch.setattr(
        runtime_assets.resources,
        "files",
        lambda _package: NonFilesystemResource(),
    )

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(
            "custom.provider",
            tmp_path / "runtime-config",
        )


def test_stage_development_assets_rejects_oversize_resource_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    package_root = tmp_path / "oversize_assets"
    package_root.mkdir()
    (package_root / "__init__.py").write_bytes(b"")
    for name in EXPECTED_RESOURCES:
        (package_root / name).write_bytes(
            b"x" * (MAXIMUM_RESOURCE_BYTES + 1)
            if name == "Caddyfile"
            else f"safe-{name}\n".encode()
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(
            "oversize_assets",
            tmp_path / "runtime-config",
        )

    assert not (tmp_path / "runtime-config").exists()


def test_caddy_entrypoint_stages_runtime_files_as_uid_10000(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for the non-root Caddy entrypoint test")
    package = importlib.resources.files(RESOURCE_PACKAGE)
    entrypoint = Path(os.fspath(package.joinpath("caddy-entrypoint.sh")))
    secrets_root = tmp_path / "secrets"
    secrets_root.mkdir(mode=0o755)
    required = {
        "controller-server-certificate": b"certificate\n",
        "controller-server-key": b"private-key\n",
        "agent-ca-certificate": b"agent-ca\n",
        "agent-proxy-auth": b"A" * 43 + b"\n",
        "management-cidrs": b"192.0.2.0/24\n",
    }
    for name, content in required.items():
        target = secrets_root / name
        target.write_bytes(content)
        target.chmod(0o444)

    command = (
        "docker",
        "run",
        "--rm",
        "--user",
        "10000:10000",
        "--tmpfs",
        "/tmp:rw,mode=1777",
        "--tmpfs",
        "/run/vonk-caddy:rw,exec,mode=0700,uid=10000,gid=10000",
        "--mount",
        f"type=bind,src={entrypoint},dst=/entrypoint.sh,readonly",
        "--mount",
        f"type=bind,src={secrets_root},dst=/run/secrets,readonly",
        "--env",
        "VONK_CONTROL_HOSTNAME=vonk-forge.tailnet.test.ts.net",
        "--env",
        "VONK_AGENT_ENROLL_HOSTNAME=enroll.test",
        "--env",
        "VONK_AGENT_HOSTNAME=agent.test",
        "--env",
        "VONK_BACKEND_PORT=8443",
        "--entrypoint",
        "/bin/sh",
        "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
        "-c",
        (
            "exec /bin/sh /entrypoint.sh /bin/sh -c '"
            'test "$(stat -c %a /tmp/vonk-agent-proxy-auth.caddy)" = 400 '
            '&& test "$(wc -l < /tmp/vonk-agent-proxy-auth.caddy)" = 1 '
            '&& test "$(stat -c %a /run/vonk-caddy/caddy)" = 500 '
            "&& /run/vonk-caddy/caddy version >/dev/null'"
        ),
    )
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "A" * 32 not in result.stdout + result.stderr

    invalid_command = list(command)
    invalid_command[invalid_command.index(
        "VONK_CONTROL_HOSTNAME=vonk-forge.tailnet.test.ts.net"
    )] = "VONK_CONTROL_HOSTNAME=control.test.example"
    invalid = subprocess.run(
        invalid_command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )
    assert invalid.returncode == 64
    assert "browser hostname must be vonk-forge.<tailnet-name>.ts.net" in invalid.stderr


def test_litellm_supervisor_materializes_file_secrets_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _litellm_supervisor()
    secret_values = {
        "os.environ/LITELLM_MASTER_KEY": "master-file-sentinel",
        "os.environ/LITELLM_UPSTREAM_KEY": "upstream-file-sentinel",
    }
    secret_paths: dict[str, Path] = {}
    for marker, value in secret_values.items():
        path = tmp_path / marker.removeprefix("os.environ/").lower()
        path.write_text(value + "\n", encoding="utf-8")
        secret_paths[marker] = path
        monkeypatch.setenv(marker.removeprefix("os.environ/"), "environment-leak")
    source = _canonical_json(_litellm_document())
    destination = tmp_path / "effective.json"
    monkeypatch.setattr(supervisor, "SECRET_FILES", secret_paths)

    effective = supervisor._materialize_config(source, destination=destination)

    assert effective == destination
    document = json.loads(destination.read_bytes())
    assert document["general_settings"] == {
        "disable_admin_ui": True,
        "master_key": secret_values["os.environ/LITELLM_MASTER_KEY"],
        "store_model_in_db": False,
    }
    assert (
        document["model_list"][0]["litellm_params"]["api_key"]
        == (secret_values["os.environ/LITELLM_UPSTREAM_KEY"])
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert all(
        os.environ[name.removeprefix("os.environ/")] == "environment-leak"
        for name in secret_values
    )


@pytest.mark.parametrize(
    ("marker", "fault"),
    (
        ("os.environ/LITELLM_MASTER_KEY", "wrong-position"),
        ("os.environ/LITELLM_MASTER_KEY", "duplicate"),
        ("os.environ/LITELLM_DATABASE_URL", "wrong-position"),
        ("os.environ/LITELLM_DATABASE_URL", "duplicate"),
        ("os.environ/LITELLM_UPSTREAM_KEY", "wrong-position"),
        ("os.environ/LITELLM_UPSTREAM_KEY", "duplicate"),
    ),
)
def test_litellm_supervisor_rejects_privileged_markers_outside_exact_schema_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str, fault: str
) -> None:
    supervisor = _litellm_supervisor()
    document = _litellm_document()
    router = document["router_settings"]
    assert isinstance(router, dict)
    if fault == "wrong-position":
        general = document["general_settings"]
        models = document["model_list"]
        assert isinstance(general, dict)
        assert isinstance(models, list) and isinstance(models[0], dict)
        if marker == "os.environ/LITELLM_MASTER_KEY":
            general["master_key"] = "literal-master-key"
            router["routing_strategy"] = marker
        elif marker == "os.environ/LITELLM_DATABASE_URL":
            general["database_url"] = "postgresql://literal/database"
            router["routing_strategy"] = marker
        else:
            parameters = models[0]["litellm_params"]
            assert isinstance(parameters, dict)
            parameters["api_key"] = "literal-upstream-key"
            models[0]["model_name"] = marker
    else:
        router["duplicate_privileged_marker"] = marker
    monkeypatch.setattr(
        supervisor,
        "_read_secret",
        lambda _path: (_ for _ in ()).throw(AssertionError("must validate first")),
    )

    with pytest.raises(RuntimeError, match="selected config is invalid"):
        supervisor._materialize_config(
            _canonical_json(document),
            destination=tmp_path / "effective.json",
        )


def test_litellm_supervisor_materializes_the_exact_verified_bytes_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _litellm_supervisor()
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    verified = _canonical_json(_litellm_document(model_name="verified"))
    selected = _active_litellm_bundle(
        supervisor,
        tmp_path,
        verified,
        now=now,
    )
    secret_values = {
        "os.environ/LITELLM_MASTER_KEY": "master-file-sentinel",
        "os.environ/LITELLM_UPSTREAM_KEY": "upstream-file-sentinel",
    }
    secret_paths: dict[str, Path] = {}
    for marker, value in secret_values.items():
        path = tmp_path / hashlib.sha256(marker.encode()).hexdigest()
        path.write_text(value + "\n", encoding="utf-8")
        secret_paths[marker] = path
    monkeypatch.setattr(supervisor, "SECRET_FILES", secret_paths)

    request = supervisor._active_request(now=now)
    assert request is not None
    selected.write_bytes(_canonical_json(_litellm_document(model_name="swapped")))
    destination = tmp_path / "effective.json"
    supervisor._materialize_config(request.config_bytes, destination=destination)

    effective = json.loads(destination.read_bytes())
    assert effective["model_list"][0]["model_name"] == "verified"
    assert request.config_sha256 == hashlib.sha256(verified).hexdigest()
