from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from vonk_control import runtime_init
from vonk_control.runtime_init import (
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


@pytest.mark.built_image
@pytest.mark.lane  # Uses the shipped runtime and its canonical protocol dependency.
def test_optional_huggingface_secret_handles_bind_mounted_dev_null_in_container(
    tmp_path: Path,
    controller_image: str,
) -> None:
    destination = tmp_path / "normalized" / "hf-token"
    command = (
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
            "--entrypoint",
            "python",
            "-v",
            "/dev/null:/run/secrets/hf-token:ro",
            "-v",
            f"{tmp_path}:/tmp/normalized",
            controller_image,
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
    (source / "prometheus").mkdir(parents=True)
    (source / "caddy").mkdir()
    (source / "caddy/Caddyfile").write_text("new relay\n")
    (source / "prometheus/prometheus.yml").write_text("{}\n")
    destination = tmp_path / "volume"
    (destination / "caddy").mkdir(parents=True)
    (destination / "caddy/Caddyfile").write_text("old relay\n")
    (destination / "retired").mkdir()
    (destination / "retired/old.yml").write_text("gone\n")

    runtime_init.write_runtime_asset_inventory(source)
    stage_runtime_assets(source, destination)

    staged = {
        path.relative_to(destination).as_posix(): path
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert set(staged) == {"caddy/Caddyfile", "prometheus/prometheus.yml"}
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


def test_shared_volume_preparation_rejects_symlinked_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
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

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        prepare_shared_volumes(paths)

    assert list(outside.iterdir()) == []
    routes.unlink()
    prepare_shared_volumes(paths)
    assert routes.is_dir() and not routes.is_symlink()
    assert list(outside.iterdir()) == []


def test_gateway_master_key_is_staged_only_outside_worker_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches copying API gateway authority into the worker's normalized mount.
    source, shared = tmp_path / "source", tmp_path / "normalized"
    source.mkdir()
    shared.mkdir()
    master = source / "litellm-master-key"
    master.write_bytes(b"test-master-key")
    stale = shared / "gateway-litellm-master-key"
    stale.write_bytes(b"previous-shared-key")
    gateway_root = tmp_path / "gateway"
    gateway_root.mkdir()
    api_key = gateway_root / runtime_init.GATEWAY_MASTER_KEY_PATH.relative_to(
        "/gateway-secrets"
    )
    # This host test cannot assign the container's group. The Linux process
    # acceptance exercises the real uid/gid boundary.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    chown = os.chown

    def preserve_host_owner(path, *args, **kwargs):
        assert path != gateway_root, "staging must preserve the gateway bind owner"
        return chown(path, *args, **kwargs)

    monkeypatch.setattr(os, "chown", preserve_host_owner)
    staged: list[tuple[Path, int, int]] = []
    stage = runtime_init.stage_private_key

    def record(source, destination, **kwargs):
        if source == master:
            staged.append((destination, kwargs["owner_uid"], kwargs["mode"]))
            return stage(
                source,
                destination,
                owner_uid=os.geteuid(),
                owner_gid=os.getegid(),
                mode=kwargs["mode"],
                preserve_parent=kwargs.get("preserve_parent", False),
            )
        return destination

    monkeypatch.setattr(runtime_init, "stage_private_key", record)
    monkeypatch.setattr(
        runtime_init, "_stage_optional_private_key", lambda *_a, **_k: None
    )
    stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert api_key.read_bytes() == master.read_bytes()
    assert api_key.stat().st_uid == gateway_root.stat().st_uid
    assert stat.S_IMODE(api_key.stat().st_mode) == 0o640
    assert stat.S_IMODE(api_key.parent.stat().st_mode) == 0o770
    assert api_key.parent.stat().st_uid == gateway_root.stat().st_uid
    assert not stale.exists()
    assert staged == [
        (api_key, gateway_root.stat().st_uid, 0o640),
        (shared / "litellm-master-key", 10002, 0o400),
    ]
    api_key.parent.chmod(0o755)
    stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert stat.S_IMODE(api_key.parent.stat().st_mode) == 0o770
    stale.write_bytes(b"previous-shared-key")
    with monkeypatch.context() as patch:

        def unavailable(source, destination, **kwargs):
            if destination == api_key:
                raise OSError("authority publication unavailable")
            return record(source, destination, **kwargs)

        patch.setattr(runtime_init, "stage_private_key", unavailable)
        with pytest.raises(Exception):  # noqa: B017 -- check publication and retirement effects
            stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert stale.read_bytes() == b"previous-shared-key"
    assert api_key.read_bytes() == master.read_bytes()
    stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert not stale.exists()

    from vonk_control.gateway_keys import MASTER_KEY_FILE

    assert MASTER_KEY_FILE == runtime_init.GATEWAY_MASTER_KEY_PATH


def test_existing_object_store_directories_stop_new_files_being_copy_on_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A store created before the attribute was set keeps its old directories,
    # and a directory only passes the attribute to files created after it.
    root = tmp_path / "model-cache"
    (root / "objects" / "ab").mkdir(parents=True)
    (root / "partials" / "set-one").mkdir(parents=True)
    (root / "partials" / "set-one" / "x.part").write_bytes(b"x")
    (root / "outside-link").symlink_to(tmp_path)
    marked: list[str] = []
    monkeypatch.setattr(
        runtime_init,
        "_disable_copy_on_write",
        lambda directory: marked.append(directory.relative_to(root).as_posix()),
    )

    runtime_init._disable_copy_on_write_below(root)

    assert sorted(marked) == ["objects", "partials", "partials/set-one"]


@pytest.mark.parametrize("fault", ["missing-inventory", "replace-unavailable"])
def test_public_runtime_assets_end_unknown_preserve_previous_and_allow_fresh_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    source = tmp_path / "image"
    source.mkdir()
    destination = tmp_path / "volume"
    destination.mkdir()
    previous = destination / "config"
    previous.write_text("verified previous")
    replace = os.replace
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise OSError("storage temporarily unavailable")

    if fault == "replace-unavailable":
        (source / "config").write_text("current kit")
        runtime_init.write_runtime_asset_inventory(source)
        monkeypatch.setattr(os, "replace", unavailable)
    ended = False
    try:
        stage_runtime_assets(source, destination)
    except Exception:  # noqa: BLE001 - observe bounded ending without asserting taxonomy
        ended = True
    assert ended
    assert previous.read_text() == "verified previous"
    assert len(calls) == (3 if fault == "replace-unavailable" else 0)
    monkeypatch.setattr(os, "replace", replace)
    (source / "config").write_text("current kit")
    runtime_init.write_runtime_asset_inventory(source)
    stage_runtime_assets(source, destination)
    assert previous.read_text() == "current kit"
    assert not list(destination.glob(".*.new"))


def test_partial_public_inventory_preserves_omitted_member_and_healthy_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "fchown", lambda *_: None)
    source, destination = tmp_path / "kit", tmp_path / "volume"
    source.mkdir()
    destination.mkdir()
    for name in ("healthy", "vanished"):
        (source / name).write_text("new")
        (destination / name).write_text("previous")
    runtime_init.write_runtime_asset_inventory(source)
    (source / "vanished").unlink()
    ended = False
    try:
        stage_runtime_assets(source, destination)
    except Exception:  # noqa: BLE001 - observe bounded ending without asserting taxonomy
        ended = True
    assert ended
    assert (destination / "healthy").read_text() == "previous"
    assert (destination / "vanished").read_text() == "previous"
    (source / "vanished").write_text("new")
    stage_runtime_assets(source, destination)
    assert (destination / "vanished").read_text() == "new"
    assert (destination / "healthy").read_text() == "new"


@pytest.mark.parametrize("fault", ["read", "parent", "write", "replace", "cleanup"])
def test_direct_private_staging_ends_preserves_and_fresh_request_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    source, destination = tmp_path / "key", tmp_path / "volume" / "key"
    source.write_bytes(b"current verified secret")
    destination.parent.mkdir()
    destination.write_bytes(b"previous secret")
    calls = []
    with monkeypatch.context() as patch:

        def unavailable(*args, **kwargs):
            calls.append(args)
            raise OSError("temporary storage failure")

        if fault == "read":
            patch.setattr(os, "read", unavailable)
        elif fault == "parent":
            patch.setattr(Path, "mkdir", unavailable)
        elif fault == "write":

            def no_progress(*args):
                calls.append(args)
                return 0

            patch.setattr(os, "write", no_progress)
        else:
            patch.setattr(os, "replace", unavailable)
            if fault == "cleanup":
                patch.setattr(Path, "unlink", unavailable)
        ended = False
        try:
            stage_private_key(
                source, destination, owner_uid=os.geteuid(), owner_gid=os.getegid()
            )
        except Exception:  # noqa: BLE001 - observe bounded ending without asserting taxonomy
            ended = True
        assert ended
        assert destination.read_bytes() == b"previous secret"
        assert 3 <= len(calls) <= 6
    stage_private_key(
        source, destination, owner_uid=os.geteuid(), owner_gid=os.getegid()
    )
    assert destination.read_bytes() == b"current verified secret"


def test_kit_inventory_assembly_ends_preserves_and_fresh_assembly_stages(
    tmp_path, monkeypatch
):
    from vonk_control.runtime_init import write_runtime_asset_inventory

    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "one").write_bytes(b"one")
    (kit / "two").write_bytes(b"two")
    assert write_runtime_asset_inventory(kit)
    previous = (kit / ".inventory.json").read_bytes()
    observed = []
    original = Path.lstat

    def unavailable(path, *args, **kwargs):
        if path == kit / "two":
            observed.append(True)
            raise OSError("kit member observation unavailable")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", unavailable)
        assert not write_runtime_asset_inventory(kit)
        assert len(observed) == 3
        assert (kit / ".inventory.json").read_bytes() == previous
    assert write_runtime_asset_inventory(kit)
    destination = tmp_path / "staged"
    stage_runtime_assets(kit, destination)
    assert (destination / "one").read_bytes() == b"one"
    assert (destination / "two").read_bytes() == b"two"


def test_compose_optional_secret_observation_ends_preserves_and_fresh_staging_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = tmp_path / "secrets", tmp_path / "normalized"
    source.mkdir()
    destination.mkdir()
    optional = source / "litellm-upstream-key"
    optional.write_bytes(b"")
    projected = destination / "litellm-upstream-key"
    projected.write_bytes(b"previous secret")
    # Other projections are independent; exercise the actual Compose staging
    # owner and optional-secret observation/cleanup boundary.
    monkeypatch.setattr(runtime_init, "stage_private_key", lambda *_a, **_k: None)
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    original = Path.lstat
    attempts = []
    with monkeypatch.context() as patch:

        def unavailable(path, *args, **kwargs):
            if path == optional:
                attempts.append(True)
                raise PermissionError("optional source observation unavailable")
            return original(path, *args, **kwargs)

        patch.setattr(Path, "lstat", unavailable)
        ended = False
        try:
            stage_compose_secrets(
                source,
                destination,
                gateway_master_key=tmp_path / "gateway" / "master-key",
            )
        except Exception:  # noqa: BLE001 -- bound, preservation and fresh completion
            ended = True
        assert ended
        assert len(attempts) == 3
        assert projected.read_bytes() == b"previous secret"
    stage_compose_secrets(
        source, destination, gateway_master_key=tmp_path / "gateway" / "master-key"
    )
    assert not projected.exists()
