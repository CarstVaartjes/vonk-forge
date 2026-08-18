from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_agent.packages.materialize import (
    MaterializationCancelled,
    MaterializationError,
    MaterializedGeneration,
    Materializer,
)
from vonk_agent_protocol.workload_packages import (
    ComponentDescriptor,
    OciBundleMetadata,
    PackageReleaseLock,
)


@dataclass(frozen=True)
class StoredObject:
    digest: str
    size: int
    kind: str
    relative_name: str


class ObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "objects").mkdir(parents=True)

    def add(self, content: bytes, *, kind: str = "blob") -> StoredObject:
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "objects" / digest
        path.write_bytes(content)
        path.chmod(0o444)
        return StoredObject(digest, len(content), kind, f"objects/{digest}")

    def object_path(self, value: StoredObject) -> Path:
        return self.root / value.relative_name


class CoordinatedObjectStore(ObjectStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.first_opened = threading.Event()
        self.second_opened = threading.Event()
        self.release_first = threading.Event()
        self.calls = 0

    def object_path(self, value: StoredObject) -> Path:
        self.calls += 1
        if self.calls == 1:
            self.first_opened.set()
            assert self.release_first.wait(2)
        elif self.calls == 2:
            self.second_opened.set()
            self.release_first.set()
        return super().object_path(value)


def _archive(entries: list[tuple[str, bytes, int]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _special_archive(kind: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        if kind == "duplicate":
            for content in (b"one", b"two"):
                info = tarfile.TarInfo("same")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        else:
            info = tarfile.TarInfo("unsafe")
            info.type = {
                "device": tarfile.CHRTYPE,
                "fifo": tarfile.FIFOTYPE,
                "symlink": tarfile.SYMTYPE,
            }[kind]
            if kind == "symlink":
                info.linkname = "elsewhere"
            archive.addfile(info)
    return output.getvalue()


def _descriptor(
    name: str,
    value: StoredObject,
    method: str,
    *,
    unpacked_size: int | None = None,
) -> ComponentDescriptor:
    return ComponentDescriptor.parse(
        {
            "name": name,
            "kind": value.kind,
            "media_type": "application/octet-stream",
            "sources": [
                {
                    "provider": "https",
                    "url": f"https://packages.example.invalid/{name}",
                }
            ],
            "digest": f"sha256:{value.digest}",
            "size": value.size,
            "unpacked_size": unpacked_size if unpacked_size else None,
            "platforms": ["linux/arm64"],
            "materialization": {"method": method},
            "evidence": [],
        }
    )


def _descriptor_document(value: ComponentDescriptor) -> dict[str, object]:
    return {
        "name": value.name,
        "kind": value.kind,
        "media_type": value.media_type,
        "sources": [dict(item) for item in value.sources],
        "digest": value.digest,
        "size": value.size,
        "unpacked_size": value.unpacked_size,
        "platforms": list(value.platforms),
        "materialization": dict(value.materialization),
        "evidence": [dict(item) for item in value.evidence],
    }


def _lock(
    _digest: str,
    components: tuple[ComponentDescriptor, ...],
) -> PackageReleaseLock:
    adapter = _descriptor_document(components[0])
    adapter.update(
        {
            "name": "adapter",
            "kind": "adapter",
            "media_type": "application/vnd.vonk-forge.workload-adapter.v1",
            "materialization": {"method": "executable"},
        }
    )
    return PackageReleaseLock.parse(
        {
            "schema_version": 1,
            "family_id": "synthetic-future-stack",
            "upstream_version": "1.0",
            "upstream_identity": {
                "provider": "git",
                "repository": "https://git.example.invalid/future/package.git",
                "commit": "1" * 40,
            },
            "components": [_descriptor_document(item) for item in components],
            "dependency_digests": [],
            "adapter": adapter,
            "adapter_abi": 1,
            "compatibility": {
                "architectures": ["arm64"],
                "operating_systems": ["linux"],
                "required_capabilities": ["recipe-runtime-v1"],
                "minimum_storage_bytes": max(1, sum(item.size for item in components)),
            },
            "validation": [],
            "provenance": [],
            "resolver": {"name": "metadata-v1", "version": 1},
        }
    )


def test_materializes_typed_components_as_one_immutable_generation(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    configuration = store.add(b'{"workers":2}\n', kind="configuration")
    snapshot_bytes = _archive([("src/main.py", b"print('ok')\n", 0o644)])
    snapshot = store.add(snapshot_bytes, kind="snapshot")
    native_bytes = _archive([("bin/runtime", b"ELF-test", 0o755)])
    native = store.add(native_bytes, kind="native")
    image = store.add(b"oci-manifest", kind="oci")
    wheel = store.add(b"wheel-bytes", kind="wheel")
    environment_bytes = _archive(
        [("lib/python/site-packages/demo/__init__.py", b"VALUE = 1\n", 0o644)]
    )
    environment = store.add(environment_bytes, kind="python-environment")
    release_digest = "a" * 64
    lock = _lock(
        release_digest,
        (
            _descriptor("settings", configuration, "configuration"),
            _descriptor(
                "source",
                snapshot,
                "snapshot",
                unpacked_size=len(b"print('ok')\n"),
            ),
            _descriptor(
                "runtime",
                native,
                "native-archive",
                unpacked_size=len(b"ELF-test"),
            ),
            _descriptor("image", image, "oci-content"),
            _descriptor("dependency", wheel, "wheel"),
            _descriptor(
                "environment",
                environment,
                "pylock-environment",
                unpacked_size=len(b"VALUE = 1\n"),
            ),
        ),
    )

    result = Materializer(store).materialize(
        lock,
        {
            value.digest: value
            for value in (configuration, snapshot, native, image, wheel, environment)
        },
        tmp_path / "generations",
    )

    assert isinstance(result, MaterializedGeneration)
    assert result.release_digest == lock.digest
    assert result.object_digests == tuple(
        sorted(
            {
                configuration.digest,
                snapshot.digest,
                native.digest,
                image.digest,
                wheel.digest,
                environment.digest,
            }
        )
    )
    assert result.environment_digest == environment.digest
    root = tmp_path / "generations" / lock.digest
    assert (
        root / "components/settings/configuration"
    ).read_bytes() == b'{"workers":2}\n'
    assert (root / "components/source/src/main.py").read_text() == "print('ok')\n"
    assert (root / "components/runtime/bin/runtime").read_bytes() == b"ELF-test"
    assert json.loads((root / "components/image/oci-reference.json").read_text()) == {
        "digest": f"sha256:{image.digest}",
        "media_type": "application/octet-stream",
        "schema_version": 1,
    }
    assert (
        root / "components/dependency/dependency.whl"
    ).read_bytes() == b"wheel-bytes"
    assert (
        root / "components/environment/lib/python/site-packages/demo/__init__.py"
    ).read_text() == "VALUE = 1\n"
    assert (
        stat.S_IMODE((root / "components/source/src/main.py").stat().st_mode) == 0o444
    )
    assert (
        stat.S_IMODE((root / "components/runtime/bin/runtime").stat().st_mode) == 0o555
    )
    assert not list((tmp_path / "generations").glob("*.partial-*"))


def test_materializes_and_verifies_signed_oci_rootfs_bundle(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "store")
    executable = b"#!/bin/sh\necho future\n"
    rootfs_entries = [
        {"kind": "directory", "mode": 0o555, "path": "usr"},
        {"kind": "directory", "mode": 0o555, "path": "usr/bin"},
        {
            "digest": hashlib.sha256(executable).hexdigest(),
            "kind": "file",
            "mode": 0o555,
            "path": "usr/bin/server",
            "size": len(executable),
        },
    ]
    rootfs_digest = hashlib.sha256(
        (json.dumps(rootfs_entries, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    config = {"architecture": "arm64", "root": {"readonly": True}}
    config_raw = (json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n").encode()
    image_manifest = {
        "schemaVersion": 2,
        "config": {"digest": "sha256:" + hashlib.sha256(config_raw).hexdigest()},
        "layers": [],
    }
    image_manifest_raw = (json.dumps(image_manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    metadata_without_digests = {
        "schema_version": 1,
        "component": "runtime",
        "manifest_digest": "sha256:" + hashlib.sha256(image_manifest_raw).hexdigest(),
        "config_digest": "sha256:" + hashlib.sha256(config_raw).hexdigest(),
        "rootfs_digest": "sha256:" + rootfs_digest,
        "architecture": "linux-arm64",
        "runtime": "runc",
        "rootfs": "rootfs",
        "entrypoint": "usr/bin/server",
    }
    metadata = OciBundleMetadata.parse(metadata_without_digests)
    archive = _archive(
        [
            ("oci-manifest.json", image_manifest_raw, 0o644),
            ("config.json", config_raw, 0o644),
            ("rootfs/usr/bin/server", executable, 0o755),
        ]
    )
    bundle = store.add(archive, kind="oci-bundle")
    descriptor = ComponentDescriptor.parse(
        {
            "name": "runtime",
            "kind": "oci-bundle",
            "media_type": "application/vnd.oci.image.layer.v1.tar",
            "sources": [{"provider": "https", "url": "https://packages.example.invalid/runtime"}],
            "digest": "sha256:" + bundle.digest,
            "size": bundle.size,
            "unpacked_size": 4096,
            "platforms": ["linux/arm64"],
            "materialization": {"method": "oci-bundle", **metadata.to_mapping()},
            "evidence": [],
        }
    )
    lock = _lock(bundle.digest, (descriptor,))
    result = Materializer(store).materialize(
        lock, {bundle.digest: bundle, descriptor.digest.removeprefix("sha256:"): bundle}, tmp_path / "generations"
    )
    root = tmp_path / "generations" / lock.digest / "components/runtime"
    assert result.release_digest == lock.digest
    assert (root / "oci-bundle.json").exists()
    assert (root / "rootfs/usr/bin/server").stat().st_mode & 0o111


def test_consumes_the_shared_release_lock_without_a_compiled_family_catalog(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    payload = store.add(b"future-package", kind="configuration")
    adapter = store.add(b"adapter-v1", kind="adapter")

    def component(
        name: str,
        value: StoredObject,
        kind: str,
        method: str,
        media_type: str,
    ) -> dict[str, object]:
        return {
            "name": name,
            "kind": kind,
            "media_type": media_type,
            "sources": [
                {
                    "provider": "https",
                    "url": f"https://packages.example.invalid/{name}",
                }
            ],
            "digest": f"sha256:{value.digest}",
            "size": value.size,
            "unpacked_size": None,
            "platforms": ["linux/arm64"],
            "materialization": {"method": method},
            "evidence": [],
        }

    lock = PackageReleaseLock.parse(
        {
            "schema_version": 1,
            "family_id": "unknown-after-agent-build",
            "upstream_version": "1.0",
            "upstream_identity": {
                "provider": "git",
                "repository": "https://git.example.invalid/future/package.git",
                "commit": "1" * 40,
            },
            "components": [
                component(
                    "settings",
                    payload,
                    "configuration",
                    "configuration",
                    "application/json",
                )
            ],
            "dependency_digests": [],
            "adapter": component(
                "adapter",
                adapter,
                "adapter",
                "executable",
                "application/vnd.vonk-forge.workload-adapter.v1",
            ),
            "adapter_abi": 1,
            "compatibility": {
                "architectures": ["arm64"],
                "operating_systems": ["linux"],
                "required_capabilities": ["recipe-runtime-v1"],
                "minimum_storage_bytes": payload.size + adapter.size,
            },
            "validation": [],
            "provenance": [],
            "resolver": {"name": "metadata-v1", "version": 1},
        }
    )

    result = Materializer(store).materialize(
        lock,
        {payload.digest: payload, adapter.digest: adapter},
        tmp_path / "generations",
    )

    root = tmp_path / "generations" / lock.digest
    assert result.release_digest == lock.digest
    assert (
        root / "components/settings/configuration"
    ).read_bytes() == b"future-package"
    assert (root / "components/adapter/adapter").read_bytes() == b"adapter-v1"


@pytest.mark.parametrize(
    ("name", "content", "unpacked_size"),
    (
        ("traversal", _archive([("../escape", b"bad", 0o644)]), 3),
        ("absolute", _archive([("/escape", b"bad", 0o644)]), 3),
        ("setuid", _archive([("bin/tool", b"bad", 0o4755)]), 3),
        ("duplicate", _special_archive("duplicate"), 6),
        ("device", _special_archive("device"), 0),
        ("fifo", _special_archive("fifo"), 0),
        ("symlink", _special_archive("symlink"), 0),
        ("bomb", _archive([("large", b"123456", 0o644)]), 5),
    ),
)
def test_archive_attacks_never_publish_a_generation(
    tmp_path: Path,
    name: str,
    content: bytes,
    unpacked_size: int,
) -> None:
    store = ObjectStore(tmp_path / "store")
    value = store.add(content, kind="archive")
    lock = _lock(
        "b" * 64, (_descriptor(name, value, "archive", unpacked_size=unpacked_size),)
    )

    with pytest.raises(MaterializationError):
        Materializer(store).materialize(
            lock,
            {value.digest: value},
            tmp_path / "generations",
        )

    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "generations" / lock.digest).exists()


def test_restart_discards_partial_staging_and_same_lock_reuses_generation(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    value = store.add(b"stable", kind="configuration")
    lock = _lock("c" * 64, (_descriptor("settings", value, "configuration"),))
    staging = tmp_path / "generations"
    stale = staging / f"{lock.digest}.partial-stale"
    stale.mkdir(parents=True)
    (stale / "untrusted").write_text("stale")

    first = Materializer(store).materialize(lock, {value.digest: value}, staging)
    inode = (staging / lock.digest).stat().st_ino
    second = Materializer(store).materialize(lock, {value.digest: value}, staging)

    assert second == first
    assert (staging / lock.digest).stat().st_ino == inode
    assert not stale.exists()


def test_concurrent_same_lock_materialization_never_deletes_peer_staging(
    tmp_path: Path,
) -> None:
    store = CoordinatedObjectStore(tmp_path / "store")
    value = store.add(b"stable", kind="configuration")
    lock = _lock("9" * 64, (_descriptor("settings", value, "configuration"),))
    staging = tmp_path / "generations"
    results: list[MaterializedGeneration] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(
                Materializer(store).materialize(lock, {value.digest: value}, staging)
            )
        except Exception as error:  # noqa: BLE001 - cross-thread test boundary
            errors.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert store.first_opened.wait(2)
    second.start()
    if not store.second_opened.wait(0.1):
        store.release_first.set()
    first.join(2)
    second.join(2)

    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]


def test_cancellation_leaves_no_published_or_partial_generation(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "store")
    value = store.add(b"data")
    lock = _lock("d" * 64, (_descriptor("payload", value, "configuration"),))

    with pytest.raises(MaterializationCancelled):
        Materializer(store, cancelled=lambda: True).materialize(
            lock,
            {value.digest: value},
            tmp_path / "generations",
        )

    assert not (tmp_path / "generations" / lock.digest).exists()
    assert not list((tmp_path / "generations").glob("*.partial-*"))


def test_rejects_missing_objects(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    value = store.add(b"data")
    descriptor = _descriptor("payload", value, "configuration")

    with pytest.raises(MaterializationError, match="missing"):
        Materializer(store).materialize(
            _lock("e" * 64, (descriptor,)),
            {},
            tmp_path / "missing",
        )


def test_rejects_duck_typed_lock_even_when_attacker_supplies_matching_objects(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    value = store.add(b"attacker-selected")
    descriptor = _descriptor("payload", value, "configuration")
    forged = SimpleNamespace(
        digest="0" * 64,
        components=(descriptor,),
        adapter=None,
    )

    with pytest.raises(MaterializationError, match="trusted release lock"):
        Materializer(store).materialize(
            forged,
            {value.digest: value},
            tmp_path / "generations",
        )


def test_rejects_duck_typed_lock_with_nonzero_attacker_digest(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    value = store.add(b"attacker-selected")
    descriptor = _descriptor("payload", value, "configuration")
    forged = SimpleNamespace(
        digest="f" * 64,
        components=(descriptor,),
        adapter=None,
    )

    with pytest.raises(MaterializationError, match="trusted release lock"):
        Materializer(store).materialize(
            forged,
            {value.digest: value},
            tmp_path / "generations",
        )
