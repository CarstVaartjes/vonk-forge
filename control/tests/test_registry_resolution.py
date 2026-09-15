import hashlib
import json

import pytest
from vonk_control.registry_resolution import (
    ManifestEnvelope,
    RegistryResolutionError,
    resolve_public_image,
)


class Registry:
    def __init__(self, manifests, addresses=("93.184.216.34",)) -> None:
        self.manifests = manifests
        self.addresses = addresses
        self.calls = []

    def resolve(self, host: str):
        return self.addresses

    def manifest(
        self, host: str, repository: str, reference: str, *, maximum_bytes: int
    ):
        self.calls.append((host, repository, reference, maximum_bytes))
        return self.manifests[reference]


def envelope(document, *, platform=None, redirects=()):
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return ManifestEnvelope(
        body=body,
        digest="sha256:" + hashlib.sha256(body).hexdigest(),
        media_type=document["mediaType"],
        platform=platform,
        redirect_chain=redirects,
    )


def test_image_resolution_selects_linux_arm64_manifest() -> None:
    arm = envelope(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "layers": [{"digest": "sha256:" + "c" * 64, "size": 12}],
        },
        platform="linux/arm64",
    )
    amd = "sha256:" + "b" * 64
    arm_digest = arm.digest
    index = envelope(
        {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": amd,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": arm_digest,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ],
        }
    )
    transport = Registry({"1.0": index, arm_digest: arm})

    image = resolve_public_image("registry.example/acme/vllm:1.0", transport)

    assert image.reference == f"registry.example/acme/vllm@{arm_digest}"
    assert image.platform == "linux/arm64"
    assert image.compressed_bytes == 12


@pytest.mark.parametrize(
    "addresses", [("127.0.0.1",), ("10.0.0.2",), ("169.254.169.254",), ("::1",)]
)
def test_registry_resolution_rejects_non_public_destinations(addresses) -> None:
    with pytest.raises(RegistryResolutionError) as caught:
        resolve_public_image("registry.example/acme/vllm:1", Registry({}, addresses))
    assert caught.value.code == "registry.destination_forbidden"


def test_manifest_digest_and_redirect_policy_are_verified() -> None:
    value = envelope(
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "layers": []},
        platform="linux/arm64",
        redirects=("http://other.example/x",),
    )
    transport = Registry({"1": value})
    with pytest.raises(RegistryResolutionError):
        resolve_public_image("registry.example/acme/vllm:1", transport)

    bad = ManifestEnvelope(
        value.body, "sha256:" + "f" * 64, value.media_type, "linux/arm64", ()
    )
    with pytest.raises(RegistryResolutionError) as caught:
        resolve_public_image("registry.example/acme/vllm:1", Registry({"1": bad}))
    assert caught.value.code == "registry.digest_mismatch"
