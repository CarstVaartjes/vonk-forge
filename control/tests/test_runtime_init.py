from __future__ import annotations

import os
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
        1000,
        0o400,
    ) in staged


def test_runtime_assets_are_staged_for_their_unprivileged_consumers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged: list[tuple[Path, Path, int, int, int, int]] = []

    def record(
        source: Path,
        destination: Path,
        *,
        owner_uid: int = 0,
        owner_gid: int = 0,
        mode: int = 0o444,
        maximum_bytes: int,
    ) -> Path:
        staged.append((source, destination, owner_uid, owner_gid, mode, maximum_bytes))
        return destination

    monkeypatch.setattr(runtime_init, "stage_runtime_file", record)
    source = tmp_path / "nas-files"
    destination = tmp_path / "docker-volume"

    stage_runtime_assets(source, destination)

    assert len(staged) == 9
    assert {
        (item[0].relative_to(source).as_posix(), item[2], item[3], item[4])
        for item in staged
    } == {
        ("litellm/bootstrap-config.json", 10002, 10001, 0o400),
        ("litellm/entrypoint.sh", 10002, 10001, 0o400),
        ("litellm/config_supervisor.py", 10002, 10001, 0o400),
        ("prometheus/prometheus.yml", 65534, 65534, 0o400),
        ("prometheus/alerts.yaml", 65534, 65534, 0o400),
        ("grafana/provisioning/datasources/prometheus.yaml", 472, 472, 0o400),
        ("grafana/provisioning/dashboards/default.yaml", 472, 472, 0o400),
        ("grafana/dashboards/jobs.json", 472, 472, 0o400),
        ("grafana/dashboards/fleet.json", 472, 472, 0o400),
    }
    assert all(item[1] == destination / item[0].relative_to(source) for item in staged)
    assert len({item[5] for item in staged}) == 1


def test_shared_volume_preparation_preserves_each_consumer_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = {
        name: tmp_path / name.replace("_", "-")
        for name in (
            "routes",
            "supervisor",
            "workload_publication",
        )
    }
    ownership: dict[Path, tuple[int, int]] = {}
    monkeypatch.setattr(
        os,
        "fchown",
        lambda descriptor, uid, gid: ownership.__setitem__(
            Path(os.readlink(f"/proc/self/fd/{descriptor}")), (uid, gid)
        ),
    )

    prepare_shared_volumes(SharedRuntimePaths(**roots))

    assert ownership == {
        roots["routes"]: (10001, 10001),
        roots["routes"] / "generations": (10001, 10001),
        roots["supervisor"]: (10002, 10001),
        roots["workload_publication"]: (10001, 10001),
        roots["workload_publication"] / "metadata": (10003, 10001),
        roots["workload_publication"] / "targets": (10003, 10001),
    }
    assert {
        path.relative_to(tmp_path).as_posix(): path.stat().st_mode & 0o777
        for path in ownership
    } == {
        "routes": 0o750,
        "routes/generations": 0o750,
        "supervisor": 0o750,
        "workload-publication": 0o750,
        "workload-publication/metadata": 0o750,
        "workload-publication/targets": 0o750,
    }


def test_shared_volume_preparation_rejects_symlinked_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    routes = tmp_path / "routes"
    routes.symlink_to(outside, target_is_directory=True)
    paths = SharedRuntimePaths(
        routes=routes,
        supervisor=tmp_path / "supervisor",
        workload_publication=tmp_path / "workload-publication",
    )

    with pytest.raises(RuntimeSecretError, match="shared runtime directory is unsafe"):
        prepare_shared_volumes(paths)

    assert list(outside.iterdir()) == []
