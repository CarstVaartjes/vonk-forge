"""Place runtime images in the Controller's layered store for tests.

The store reads images by name and size only (bytes are verified once, at
ingress), so a test can place an image at the address and size its fixture
already uses instead of producing real registry content.
"""

from __future__ import annotations

import hashlib
import json

from vonk_control.oci_image_store import OCI_MANIFEST
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

RUNTIME_CONFIG = {
    "architecture": "arm64",
    "os": "linux",
    "config": {
        "User": "10001:10001",
        "Labels": {"ai.vonkforge.runtime-interface": "v1"},
    },
    "rootfs": {"type": "layers", "diff_ids": []},
}


def place_test_image(
    storage: FilesystemRuntimeImageStorage,
    address: str,
    image_bytes: int,
    *,
    config: dict[str, object] | None = None,
) -> None:
    """Store an image at ``address`` (manifest hex) whose layers total ``image_bytes``."""

    blobs = storage.layout.root / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    config_bytes = json.dumps(config or RUNTIME_CONFIG, sort_keys=True).encode()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    (blobs / config_digest).write_bytes(config_bytes)
    layer = hashlib.sha256(f"layer:{address}:{image_bytes}".encode()).hexdigest()
    (blobs / layer).write_bytes(b"\0" * image_bytes)
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": f"sha256:{config_digest}",
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": f"sha256:{layer}",
                "size": image_bytes,
            }
        ],
    }
    (blobs / address).write_bytes(json.dumps(manifest).encode())


def remove_test_image(storage: FilesystemRuntimeImageStorage, address: str) -> None:
    """Simulate cache loss: the image's manifest is gone."""

    (storage.layout.root / "blobs" / "sha256" / address).unlink()
