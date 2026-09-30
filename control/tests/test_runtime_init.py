from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from vonk_control import runtime_init
from vonk_control.runtime_init import (
    RuntimeSecretError,
    SharedRuntimePaths,
    prepare_shared_volumes,
    stage_compose_secrets,
    stage_private_key,
    stage_runtime_assets,
    stage_runtime_file,
)


def test_runtime_secret_can_be_staged_without_host_owner_assumptions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"private-runtime-key\n")
    source.chmod(0o660)
    destination = tmp_path / "normalized" / "runtime-private-key.pem"

    stage_private_key(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )

    assert destination.read_bytes() == b"private-runtime-key\n"
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


def test_public_runtime_file_can_use_the_larger_bounded_asset_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dashboard.json"
    source.write_bytes(b"x" * (16 * 1024 + 1))
    destination = tmp_path / "normalized" / "dashboard.json"

    stage_runtime_file(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
        mode=0o400,
    )

    assert destination.stat().st_size == 16 * 1024 + 1


def test_compose_secret_staging_gives_step_ca_its_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged: list[tuple[Path, Path, int, int, int]] = []

    def record(
        source: Path,
        destination: Path,
        *,
        owner_uid: int = 0,
        owner_gid: int = 0,
        mode: int = 0o444,
    ) -> Path:
        staged.append((source, destination, owner_uid, owner_gid, mode))
        return destination

    monkeypatch.setattr(runtime_init, "stage_private_key", record)
    source = tmp_path / "source"
    destination = tmp_path / "normalized"

    stage_compose_secrets(source, destination)

    assert (
        source / "step-ca-config",
        destination / "step-ca" / "ca.json",
        1000,
        10001,
        0o440,
    ) in staged


def test_optional_huggingface_secret_is_normalized_only_when_present(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "normalized"
    source.mkdir()
    token = source / "hf-token"
    token.write_text("hf_test_secret\n")

    runtime_init._stage_optional_private_key(
        source / "hf-token",
        destination / "hf-token",
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )

    projected = destination / "hf-token"
    assert projected.read_bytes() == b"hf_test_secret\n"
    assert projected.stat().st_mode & 0o777 == 0o400

    token.unlink()
    runtime_init._stage_optional_private_key(
        source / "hf-token", destination / "hf-token"
    )
    assert not projected.exists()


def test_optional_litellm_upstream_key_is_not_projected_when_blank(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "litellm-upstream-key"
    source.parent.mkdir()
    source.write_text("")
    destination = tmp_path / "normalized" / "litellm-upstream-key"

    runtime_init._stage_optional_private_key(source, destination)

    assert not destination.exists()


def test_optional_huggingface_secret_treats_dev_null_as_absent(tmp_path: Path) -> None:
    if not runtime_init._is_null_device(Path("/dev/null")):
        pytest.skip("host null-device identity is not the Linux /dev/null device")
    destination = tmp_path / "normalized" / "hf-token"
    destination.parent.mkdir(parents=True)
    destination.write_text("stale token")

    runtime_init._stage_optional_private_key(Path("/dev/null"), destination)

    assert not destination.exists()


@pytest.mark.lane  # Runs the module inside a Docker container.
@pytest.mark.skipif(
    os.environ.get("RUN_ORBSTACK_CONTAINER_TESTS") != "1",
    reason="OrbStack container checks are opt-in",
)
def test_optional_huggingface_secret_handles_bind_mounted_dev_null_in_container(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    source_module = Path(runtime_init.__file__).resolve()
    destination = tmp_path / "normalized" / "hf-token"
    command = (
        "import sys; sys.path.insert(0, '/tmp/module'); "
        "from pathlib import Path; "
        "from vonk_control.runtime_init import _stage_optional_private_key; "
        "destination = Path('/tmp/normalized/hf-token'); destination.parent.mkdir(parents=True, exist_ok=True); "
        "destination.write_text('stale token'); "
        "_stage_optional_private_key(Path('/run/secrets/hf-token'), destination); "
        "assert not destination.exists()"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "/dev/null:/run/secrets/hf-token:ro",
            "-v",
            f"{source_module.parent.parent}:/tmp/module:ro",
            "-v",
            f"{tmp_path}:/tmp/normalized",
            "python:3.14-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d",
            "python",
            "-c",
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not destination.exists()


def test_runtime_assets_follow_the_shipped_release_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ownership is root's in the container; the test process may not chown.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    source = tmp_path / "image"
    (source / "grafana/dashboards").mkdir(parents=True)
    (source / "caddy").mkdir()
    (source / "caddy/Caddyfile").write_text("new relay\n")
    (source / "grafana/dashboards/fleet.json").write_text("{}\n")
    destination = tmp_path / "volume"
    (destination / "caddy").mkdir(parents=True)
    (destination / "caddy/Caddyfile").write_text("old relay\n")
    (destination / "retired").mkdir()
    (destination / "retired/old.yml").write_text("gone\n")

    stage_runtime_assets(source, destination)

    staged = {
        path.relative_to(destination).as_posix(): path
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert set(staged) == {"caddy/Caddyfile", "grafana/dashboards/fleet.json"}
    assert staged["caddy/Caddyfile"].read_text() == "new relay\n"
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in staged.values())
    assert not (destination / "retired").exists()


def test_shared_volume_preparation_preserves_each_consumer_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = {
        name: tmp_path / name.replace("_", "-")
        for name in (
            "agent_artifacts",
            "model_cache",
            "routes",
            "supervisor",
            "state",
            "gateway",
        )
    }
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os,
        "fchown",
        lambda _descriptor, uid, gid: ownership.append((uid, gid)),
    )

    prepare_shared_volumes(SharedRuntimePaths(**roots))

    assert ownership == [
        (10001, 10001),
        (10001, 10001),
        (10001, 10001),
        (10001, 10001),
        (10001, 10001),
        (10002, 10001),
        (-1, 10001),
    ]
    expected_paths = (
        roots["state"],
        roots["agent_artifacts"],
        roots["model_cache"],
        roots["routes"],
        roots["routes"] / "generations",
        roots["supervisor"],
        roots["gateway"],
    )
    assert {
        path.relative_to(tmp_path).as_posix(): path.stat().st_mode & 0o777
        for path in expected_paths
    } == {
        "state": 0o750,
        "agent-artifacts": 0o750,
        "model-cache": 0o750,
        "routes": 0o750,
        "routes/generations": 0o750,
        "supervisor": 0o750,
        "gateway": 0o770,
    }


def test_shared_volume_preparation_rejects_symlinked_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    routes = tmp_path / "routes"
    routes.symlink_to(outside, target_is_directory=True)
    paths = SharedRuntimePaths(
        agent_artifacts=tmp_path / "agent-artifacts",
        model_cache=tmp_path / "model-cache",
        routes=routes,
        supervisor=tmp_path / "supervisor",
        state=tmp_path / "state",
        gateway=tmp_path / "gateway",
    )

    with pytest.raises(RuntimeSecretError, match="shared runtime directory is unsafe"):
        prepare_shared_volumes(paths)

    assert list(outside.iterdir()) == []


def test_controller_gets_its_own_readable_master_key_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged: list[tuple[Path, Path, int, int, int]] = []

    def record(source, destination, *, owner_uid=0, owner_gid=0, mode=0o444):
        staged.append((source, destination, owner_uid, owner_gid, mode))
        return destination

    monkeypatch.setattr(runtime_init, "stage_private_key", record)
    monkeypatch.setattr(runtime_init, "_stage_optional_private_key", record)

    stage_compose_secrets(Path("/src"), Path("/dst"))

    master = Path("/src/litellm-master-key")
    copies = {
        (dest.name, uid, mode) for src, dest, uid, _, mode in staged if src == master
    }
    assert copies == {
        ("litellm-master-key", 10002, 0o400),
        ("gateway-litellm-master-key", 10001, 0o400),
    }
    from vonk_control.gateway_keys import MASTER_KEY_FILE
    from vonk_control.settings import SECRETS_ROOT

    assert MASTER_KEY_FILE == SECRETS_ROOT / "gateway-litellm-master-key"
