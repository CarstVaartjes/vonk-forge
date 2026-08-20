from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import replace
from importlib import resources
from pathlib import Path

import jsonschema
import pytest

from cluster_profiles.platform_release import OciDeploymentBundle

ROOT = Path(__file__).resolve().parents[2]
ASSETS = (
    "Caddyfile",
    "bin/harden-hermes-egress",
    "caddy/entrypoint.sh",
    "compose.yaml",
    "grafana/dashboards/fleet.json",
    "grafana/dashboards/jobs.json",
    "grafana/provisioning/dashboards/default.yaml",
    "grafana/provisioning/datasources/prometheus.yaml",
    "hermes-agent/compose.yaml",
    "images.lock.json",
    "litellm/bootstrap-config.json",
    "litellm/config.yaml",
    "litellm/config_supervisor.py",
    "litellm/entrypoint.sh",
    "prometheus/alerts.yaml",
    "prometheus/prometheus.yml",
    "postgres/init-databases.sh",
    "registry/config.yml",
    "step-ca/ca.json",
    "tailscale/compose.yaml",
    "tailscale/configure.sh",
    "trust/litellm-cosign.pub",
)
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_LAYER_MEDIA_TYPE = "application/vnd.vonk-forge.control-deployment.v1.tar"


def _bundle_module():
    return importlib.import_module("cluster_profiles.deployment_bundle")


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _descriptor(raw: bytes) -> OciDeploymentBundle:
    manifest = b"canonical OCI manifest fixture"
    manifest_digest = _digest(manifest)
    return OciDeploymentBundle(
        reference=("registry.example/vonk-forge/control-deployment@" + manifest_digest),
        manifest_digest=manifest_digest,
        manifest_size=len(manifest),
        manifest_media_type=OCI_MANIFEST_MEDIA_TYPE,
        layer_digest=_digest(raw),
        layer_size=len(raw),
        layer_media_type=OCI_LAYER_MEDIA_TYPE,
    )


def _copy_source(tmp_path: Path) -> Path:
    source = tmp_path / "compose"
    for relative in ASSETS:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "deploy/compose" / relative, target)
        target.chmod(
            0o755
            if relative
            in {
                "litellm/config_supervisor.py",
                "litellm/entrypoint.sh",
                "postgres/init-databases.sh",
                "bin/harden-hermes-egress",
            }
            else 0o644
        )
    return source


def _rebuilt_archive(raw: bytes, *, mutate) -> bytes:
    entries: list[tuple[tarfile.TarInfo, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            assert extracted is not None
            entries.append((member, extracted.read()))
    mutate(entries)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for member, content in entries:
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def _raw_archive(entries: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for member, content in entries:
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def _regular_member(name: str, content: bytes) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    member.size = len(content)
    return member, content


def test_bundle_is_deterministic_and_binds_every_runtime_asset() -> None:
    module = _bundle_module()

    first = module.build_deployment_bundle(ROOT / "deploy/compose")
    second = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(first, _descriptor(first))

    assert first == second
    assert tuple(verified.files) == ASSETS
    assert verified.archive_sha256 == hashlib.sha256(first).hexdigest()
    assert len(verified.manifest_sha256) == 64


def test_development_compose_is_source_only_and_absent_from_bundle() -> None:
    module = _bundle_module()
    source = ROOT / "deploy/compose"

    assert (source / "compose.dev.yaml").is_file()
    assert (source / "compose.dev.images.yaml").is_file()
    raw = module.build_deployment_bundle(source)
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        archive_names = {member.name for member in archive.getmembers()}

    assert "compose.dev.yaml" not in verified.files
    assert "compose.dev.yaml" not in archive_names
    assert "compose.dev.images.yaml" not in verified.files
    assert "compose.dev.images.yaml" not in archive_names


def test_bundle_ignores_interpreter_cache_artifacts(tmp_path: Path) -> None:
    module = _bundle_module()
    source = tmp_path / "compose"
    shutil.copytree(ROOT / "deploy/compose", source)
    cache = source / "litellm/__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "config_supervisor.cpython-312.pyc").write_bytes(b"generated")

    assert module.build_deployment_bundle(source) == module.build_deployment_bundle(
        ROOT / "deploy/compose"
    )


def test_bundle_schema_is_packaged_and_matches_repository_copy() -> None:
    _bundle_module()
    repository = (ROOT / "schemas/control-deployment-bundle.schema.json").read_bytes()
    packaged = (
        resources.files("cluster_profiles")
        .joinpath("schemas", "control-deployment-bundle.schema.json")
        .read_bytes()
    )

    assert packaged == repository


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "/absolute",
        "./relative",
        "a/./relative",
        "a/../escape",
        "a\\windows",
        "a//noncanonical",
        "a/",
    ),
)
def test_bundle_schema_rejects_unsafe_or_noncanonical_paths(
    unsafe_path: str,
) -> None:
    schema = json.loads(
        (ROOT / "schemas/control-deployment-bundle.schema.json").read_text()
    )
    document = {
        "files": [
            {
                "mode": 420,
                "path": unsafe_path,
                "sha256": "a" * 64,
                "size": 1,
            }
        ],
        "format": "vonk-control-deployment-bundle-v1",
        "schema_version": 1,
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(document)


def test_bundle_schema_rejects_duplicate_file_entries() -> None:
    schema = json.loads(
        (ROOT / "schemas/control-deployment-bundle.schema.json").read_text()
    )
    entry = {
        "mode": 420,
        "path": "Caddyfile",
        "sha256": "a" * 64,
        "size": 1,
    }
    document = {
        "files": [entry, dict(entry)],
        "format": "vonk-control-deployment-bundle-v1",
        "schema_version": 1,
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(document)


def test_bundle_allowlist_covers_every_local_production_compose_asset() -> None:
    compose_root = ROOT / "deploy/compose"
    local_assets: set[str] = set()
    for relative in (
        "compose.yaml",
        "hermes-agent/compose.yaml",
        "tailscale/compose.yaml",
    ):
        compose_text = (compose_root / relative).read_text()
        compose_parent = (compose_root / relative).parent
        for include_path in re.findall(
            r"(?m)^\s*-\s+([A-Za-z0-9._/-]+/compose\.yaml)\s*$",
            compose_text,
        ):
            local_assets.add(
                (compose_parent / include_path).relative_to(compose_root).as_posix()
            )
        local_sources = re.findall(
            r"(?m)^\s*-\s+\./([^:\s]+)", compose_text
        ) + re.findall(r"(?m)^\s+source:\s+\./([^:\s]+)", compose_text)
        for source in local_sources:
            source_path = compose_parent / source
            if source_path.is_dir():
                local_assets.update(
                    child.relative_to(compose_root).as_posix()
                    for child in source_path.rglob("*")
                    if child.is_file() and "__pycache__" not in child.parts
                )
            else:
                local_assets.add(source_path.relative_to(compose_root).as_posix())

    assert local_assets <= set(ASSETS)


def test_verified_bundle_extracts_exact_bytes_and_modes(tmp_path: Path) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))
    destination = tmp_path / "generation-assets"

    module.extract_deployment_bundle(raw, destination, verified)

    for relative in ASSETS:
        extracted = destination / relative
        assert (
            extracted.read_bytes() == (ROOT / "deploy/compose" / relative).read_bytes()
        )
        expected_mode = (
            0o755
            if relative
                in {
                    "bin/harden-hermes-egress",
                    "litellm/config_supervisor.py",
                    "litellm/entrypoint.sh",
                    "postgres/init-databases.sh",
                }
            else 0o644
        )
        assert extracted.stat().st_mode & 0o777 == expected_mode
    assert (destination / "deployment-bundle.json").is_file()


def test_builder_rejects_missing_extra_or_symlinked_inputs(tmp_path: Path) -> None:
    module = _bundle_module()
    source = _copy_source(tmp_path)
    (source / ASSETS[0]).unlink()
    with pytest.raises(module.DeploymentBundleError, match="missing"):
        module.build_deployment_bundle(source)

    source = _copy_source(tmp_path / "extra")
    (source / "unexpected.conf").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(module.DeploymentBundleError, match="unexpected"):
        module.build_deployment_bundle(source)

    source = _copy_source(tmp_path / "link")
    target = source / ASSETS[0]
    target.unlink()
    target.symlink_to(ROOT / "deploy/compose" / ASSETS[0])
    with pytest.raises(module.DeploymentBundleError, match="unsafe"):
        module.build_deployment_bundle(source)


def test_builder_keeps_open_parent_when_source_directory_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch resolving a trusted leaf through a newly swapped parent link."""

    module = _bundle_module()
    source = _copy_source(tmp_path)
    trusted = (source / "caddy/entrypoint.sh").read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "entrypoint.sh").write_bytes(b"#!/bin/sh\necho attacker\n")
    (outside / "entrypoint.sh").chmod(0o644)
    original_open = module.os.open
    swapped = False

    def swap_parent_before_leaf(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and Path(path).name == "entrypoint.sh":
            swapped = True
            (source / "caddy").rename(source / "caddy-original")
            (source / "caddy").symlink_to(outside, target_is_directory=True)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", swap_parent_before_leaf)
    raw = module.build_deployment_bundle(source)
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))
    destination = tmp_path / "extracted"
    module.extract_deployment_bundle(raw, destination, verified)

    assert swapped
    assert (destination / "caddy/entrypoint.sh").read_bytes() == trusted


def test_builder_rejects_special_source_entries_without_opening_them(
    tmp_path: Path,
) -> None:
    module = _bundle_module()
    source = _copy_source(tmp_path)
    os.mkfifo(source / "unexpected-pipe")

    with pytest.raises(module.DeploymentBundleError, match="unsafe"):
        module.build_deployment_bundle(source)


def test_builder_bounds_source_directory_depth(tmp_path: Path) -> None:
    module = _bundle_module()
    source = _copy_source(tmp_path)
    directory = source
    for _index in range(65):
        directory /= "d"
        directory.mkdir()

    with pytest.raises(module.DeploymentBundleError, match="depth"):
        module.build_deployment_bundle(source)


def test_builder_bounds_source_entry_count(tmp_path: Path) -> None:
    module = _bundle_module()
    source = _copy_source(tmp_path)
    for index in range(4097):
        (source / f"empty-{index:04d}").mkdir()

    with pytest.raises(module.DeploymentBundleError, match="too many entries"):
        module.build_deployment_bundle(source)


def test_verifier_rejects_descriptor_mismatch_and_modified_bytes() -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    descriptor = _descriptor(raw)

    with pytest.raises(module.DeploymentBundleError, match="size"):
        module.verify_deployment_bundle(
            raw, replace(descriptor, layer_size=len(raw) + 1)
        )
    modified = bytearray(raw)
    modified[1024] ^= 1
    with pytest.raises(module.DeploymentBundleError, match="digest"):
        module.verify_deployment_bundle(bytes(modified), descriptor)


def test_verifier_rejects_non_bytes_with_a_bounded_domain_error() -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")

    with pytest.raises(module.DeploymentBundleError, match="immutable bytes"):
        module.verify_deployment_bundle("not bytes", _descriptor(raw))


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"manifest_size": 0}, "manifest size"),
        ({"manifest_media_type": "application/example"}, "manifest media type"),
        ({"layer_media_type": "application/example"}, "layer media type"),
        ({"layer_digest": "sha256:" + "A" * 64}, "layer digest"),
    ),
)
def test_verifier_validates_the_complete_oci_descriptor(
    change: dict[str, object], message: str
) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")

    with pytest.raises(module.DeploymentBundleError, match=message):
        module.verify_deployment_bundle(raw, replace(_descriptor(raw), **change))


def test_verifier_wraps_malformed_descriptor_field_types() -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")

    with pytest.raises(module.DeploymentBundleError, match="descriptor"):
        module.verify_deployment_bundle(raw, replace(_descriptor(raw), reference=None))


def test_verifier_rejects_noncanonical_headers(tmp_path: Path) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")

    def change_mtime(entries):
        entries[0][0].mtime = 1

    noncanonical = _rebuilt_archive(raw, mutate=change_mtime)

    with pytest.raises(module.DeploymentBundleError, match="canonical"):
        module.verify_deployment_bundle(noncanonical, _descriptor(noncanonical))


def test_verifier_rejects_duplicate_device_oversized_and_excess_members() -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")

    def duplicate_first(entries):
        original, content = entries[0]
        duplicate = tarfile.TarInfo(original.name)
        duplicate.mode = original.mode
        duplicate.size = len(content)
        entries.append((duplicate, content))

    duplicate = _rebuilt_archive(raw, mutate=duplicate_first)
    with pytest.raises(module.DeploymentBundleError, match="unsafe"):
        module.verify_deployment_bundle(duplicate, _descriptor(duplicate))

    device = tarfile.TarInfo("device")
    device.type = tarfile.CHRTYPE
    device.devmajor = 1
    device.devminor = 3
    raw_device = _raw_archive([(device, b"")])
    with pytest.raises(module.DeploymentBundleError, match="unsafe"):
        module.verify_deployment_bundle(raw_device, _descriptor(raw_device))

    oversized_content = b"x" * (16 * 1024 * 1024 + 1)
    oversized = _raw_archive([_regular_member("oversized", oversized_content)])
    with pytest.raises(module.DeploymentBundleError, match="unsafe"):
        module.verify_deployment_bundle(oversized, _descriptor(oversized))

    excessive = _raw_archive(
        [_regular_member(f"entry-{index}", b"x") for index in range(258)]
    )
    with pytest.raises(module.DeploymentBundleError, match="member"):
        module.verify_deployment_bundle(excessive, _descriptor(excessive))


@pytest.mark.parametrize("member_type", (tarfile.SYMTYPE, tarfile.LNKTYPE))
def test_verifier_rejects_archive_links(member_type: bytes) -> None:
    module = _bundle_module()
    link = tarfile.TarInfo("linked")
    link.type = member_type
    link.linkname = "Caddyfile"
    raw = _raw_archive([(link, b"")])

    with pytest.raises(module.DeploymentBundleError, match="unsafe"):
        module.verify_deployment_bundle(raw, _descriptor(raw))


def test_verifier_rejects_sparse_members_before_materializing_them(
    tmp_path: Path,
) -> None:
    module = _bundle_module()
    sparse = tmp_path / "sparse"
    with sparse.open("wb") as output:
        output.seek(1024 * 1024 - 1)
        output.write(b"x")
    archive = tmp_path / "sparse.tar"
    result = subprocess.run(
        ["tar", "--format=gnu", "--sparse", "-cf", archive, "sparse"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    raw = archive.read_bytes()
    assert len(raw) < sparse.stat().st_size

    with pytest.raises(module.DeploymentBundleError, match="sparse"):
        module.verify_deployment_bundle(raw, _descriptor(raw))


def test_verifier_wraps_deeply_nested_manifest_json() -> None:
    module = _bundle_module()
    deeply_nested = b'{"files":' + b"[" * 2000 + b"]" * 2000 + b"}"
    raw = _raw_archive([_regular_member("deployment-bundle.json", deeply_nested)])

    with pytest.raises(module.DeploymentBundleError, match="manifest"):
        module.verify_deployment_bundle(raw, _descriptor(raw))


@pytest.mark.parametrize("unsafe_name", ("../escape", "/absolute", "a/../../escape"))
def test_verifier_rejects_unsafe_archive_paths(unsafe_name: str) -> None:
    module = _bundle_module()
    manifest = {
        "files": [
            {
                "mode": 420,
                "path": unsafe_name,
                "sha256": hashlib.sha256(b"unsafe").hexdigest(),
                "size": 6,
            }
        ],
        "format": "vonk-control-deployment-bundle-v1",
        "schema_version": 1,
    }
    manifest_raw = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in (
            ("deployment-bundle.json", manifest_raw),
            (unsafe_name, b"unsafe"),
        ):
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    raw = output.getvalue()

    with pytest.raises(module.DeploymentBundleError):
        module.verify_deployment_bundle(raw, _descriptor(raw))


def test_extraction_refuses_an_existing_or_symlinked_destination(
    tmp_path: Path,
) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(module.DeploymentBundleError, match="new directory"):
        module.extract_deployment_bundle(raw, existing, verified)

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(module.DeploymentBundleError, match="new directory"):
        module.extract_deployment_bundle(raw, linked, verified)


def test_extraction_reverifies_bytes_instead_of_trusting_a_forged_receipt(
    tmp_path: Path,
) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))

    def append_unexpected(entries):
        entries.append(_regular_member("unexpected", b"attacker"))

    changed = _rebuilt_archive(raw, mutate=append_unexpected)
    forged = replace(
        verified,
        archive_sha256=hashlib.sha256(changed).hexdigest(),
    )
    destination = tmp_path / "forged"

    with pytest.raises(module.DeploymentBundleError):
        module.extract_deployment_bundle(changed, destination, forged)

    assert not destination.exists()


def test_extraction_cleans_partial_destination_so_retry_can_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))
    destination = tmp_path / "generation"
    original_write = module._write_file
    writes = 0

    def fail_second_write(parent, name, content, mode):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated write failure")
        return original_write(parent, name, content, mode)

    monkeypatch.setattr(module, "_write_file", fail_second_write)
    with pytest.raises(module.DeploymentBundleError, match="extraction"):
        module.extract_deployment_bundle(raw, destination, verified)
    assert not destination.exists()

    monkeypatch.setattr(module, "_write_file", original_write)
    module.extract_deployment_bundle(raw, destination, verified)
    assert (destination / "deployment-bundle.json").is_file()


def test_extraction_cleans_destination_when_open_after_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))
    destination = tmp_path / "generation"
    original_open = module.os.open

    def fail_destination_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == destination.name and flags & os.O_DIRECTORY:
            raise OSError("simulated directory open failure")
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", fail_destination_open)
    with pytest.raises(module.DeploymentBundleError, match="new directory"):
        module.extract_deployment_bundle(raw, destination, verified)

    assert not destination.exists()


def test_extraction_parse_failure_leaves_destination_retryable(tmp_path: Path) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))
    malformed = _raw_archive([_regular_member("deployment-bundle.json", b'{"files":[')])
    destination = tmp_path / "generation"

    with pytest.raises(module.DeploymentBundleError, match="manifest"):
        module.extract_deployment_bundle(malformed, destination, verified)
    assert not destination.exists()

    module.extract_deployment_bundle(raw, destination, verified)
    assert (destination / "deployment-bundle.json").is_file()


def test_extraction_rejects_destination_substitution_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _bundle_module()
    raw = module.build_deployment_bundle(ROOT / "deploy/compose")
    verified = module.verify_deployment_bundle(raw, _descriptor(raw))
    destination = tmp_path / "generation"
    moved = tmp_path / "moved-generation"
    original_write = module._write_relative
    swapped = False

    def substitute_after_first_write(root, relative, content, mode):
        nonlocal swapped
        original_write(root, relative, content, mode)
        if not swapped:
            swapped = True
            destination.rename(moved)
            destination.mkdir()

    monkeypatch.setattr(module, "_write_relative", substitute_after_first_write)
    with pytest.raises(module.DeploymentBundleError, match="extraction"):
        module.extract_deployment_bundle(raw, destination, verified)

    assert swapped
    assert not any(destination.iterdir())
