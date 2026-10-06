"""The layered store keeps one copy of a shared layer and only OCI manifests.

Uses the real skopeo the Controller image ships, on Docker archives shaped
like a Spark build's upload.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

import pytest
from vonk_control.oci_image_store import Collection, OciImageStore, StoredImage

pytestmark = pytest.mark.skipif(
    shutil.which("skopeo") is None, reason="needs skopeo, as in the Controller image"
)


def _stored(answer: object) -> StoredImage:
    assert isinstance(answer, StoredImage), answer
    return answer


def _layer(name: str, content: bytes) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as layer:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        layer.addfile(member, io.BytesIO(content))
    return raw.getvalue()


def _docker_archive(path: Path, layers: list[bytes]) -> Path:
    diff_ids = [f"sha256:{hashlib.sha256(layer).hexdigest()}" for layer in layers]
    config = json.dumps(
        {
            "architecture": "arm64",
            "os": "linux",
            "config": {
                "User": "10001:10001",
                "Labels": {"ai.vonkforge.runtime-interface": "v1"},
            },
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        }
    ).encode()
    config_name = f"{hashlib.sha256(config).hexdigest()}.json"
    members = {config_name: config}
    layer_names = []
    for index, layer in enumerate(layers):
        name = f"layer{index}/layer.tar"
        members[name] = layer
        layer_names.append(name)
    members["manifest.json"] = json.dumps(
        [{"Config": config_name, "RepoTags": [], "Layers": layer_names}]
    ).encode()
    with tarfile.open(path, mode="w") as archive:
        for name, payload in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return path


def test_archives_share_their_base_layer_and_read_back_complete(tmp_path: Path) -> None:
    store = OciImageStore(tmp_path / "artifacts", skopeo=shutil.which("skopeo") or "")
    base = _layer("base.bin", b"b" * 200_000)
    first = _stored(
        store.import_archive(
            _docker_archive(tmp_path / "first.tar", [base, _layer("recipe", b"one")])
        )
    )
    blobs = store.root / "blobs/sha256"
    stored_after_first = {path.name for path in blobs.iterdir()}
    second = _stored(
        store.import_archive(
            _docker_archive(tmp_path / "second.tar", [base, _layer("recipe", b"two")])
        )
    )
    added = {path.name for path in blobs.iterdir()} - stored_after_first

    assert first.layer_digests[0] == second.layer_digests[0]
    # The update adds its recipe layer, config and manifest; never the base.
    assert len(added) == 3
    assert second.layer_digests[0].removeprefix("sha256:") not in added
    manifest = json.loads(store.blob_path(second.manifest_digest).read_bytes())
    assert manifest["mediaType"] == "application/vnd.oci.image.manifest.v1+json"
    assert store.read(second.manifest_digest) == second
    assert second.stored_bytes == sum(
        store.blob_path(digest).stat().st_size for digest in second.layer_digests
    )

    store.blob_path(second.layer_digests[1]).unlink()
    assert store.read(second.manifest_digest) is None
    assert store.read(first.manifest_digest) == first


def test_a_repeated_copy_marks_its_image_fresh_for_collection(tmp_path: Path) -> None:
    store = OciImageStore(tmp_path / "artifacts", skopeo=shutil.which("skopeo") or "")
    archive = _docker_archive(tmp_path / "image.tar", [_layer("base.bin", b"b" * 64)])
    image = _stored(store.import_archive(archive))
    blobs = store.root / "blobs/sha256"
    old = time.time() - 3600
    for blob in blobs.iterdir():
        os.utime(blob, (old, old))

    # Copying the same image again writes no blob, but must not leave it
    # looking abandoned to a collection that has not seen its record yet.
    store.import_archive(archive)
    assert store.collect(lambda: (), grace_seconds=60) == Collection(0, 0)

    for blob in blobs.iterdir():
        os.utime(blob, (old, old))
    removed = store.collect(lambda: (), grace_seconds=60)
    assert isinstance(removed, Collection) and removed.blobs_removed == len(
        image.blob_digests
    )
    assert store.read(image.manifest_digest) is None
    # The layout's index still names the collected image; storing it again
    # works all the same.
    assert store.import_archive(archive) == image
