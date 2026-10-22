from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "control/Dockerfile"

SKOPEO_IMAGE = (
    "quay.io/skopeo/stable:v1.22.3-immutable@sha256:"
    "c0ee1f4edca5c01cb8d5611124f92f3cc47196ecab68aee5d0f90834e00574d5"
)
SKOPEO_INDEX = "sha256:c0ee1f4edca5c01cb8d5611124f92f3cc47196ecab68aee5d0f90834e00574d5"
SKOPEO_AMD64 = "sha256:ab4c269c9e2bd11affe2666b860fb651a15afec121c15986b052b02e09d86239"
SKOPEO_ARM64 = "sha256:916612c4c9bcf1dd633137ec4ce88987e368c16b3d611642006ca5996fe28c98"


def test_controller_image_pins_and_packages_the_reviewed_skopeo_transport() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert f"ARG SKOPEO_IMAGE={SKOPEO_IMAGE}" in dockerfile
    assert f"ARG SKOPEO_INDEX_DIGEST={SKOPEO_INDEX}" in dockerfile
    assert f"ARG SKOPEO_AMD64_DIGEST={SKOPEO_AMD64}" in dockerfile
    assert f"ARG SKOPEO_ARM64_DIGEST={SKOPEO_ARM64}" in dockerfile
    assert "FROM ${SKOPEO_IMAGE} AS skopeo" in dockerfile
    assert "ARG TARGETARCH" in dockerfile
    assert "skopeo inspect --tls-verify=true --raw" in dockerfile
    # skopeo rejects a reference carrying both a tag and a digest, so the
    # in-image resolution check must use the bare repository and index digest.
    assert "docker://quay.io/skopeo/stable@${SKOPEO_INDEX_DIGEST}" in dockerfile
    # Wrong implementation: importing Skopeo's old store overwrites fresh Debian roots.
    assert "COPY --from=skopeo /etc/ssl /etc/ssl" not in dockerfile
    assert (
        "ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt"
        in dockerfile
    )
    assert "architecture" in dockerfile
    assert '"$TARGETARCH"' in dockerfile
    assert 'expected_child="$SKOPEO_ARM64_DIGEST"' in dockerfile
    assert (
        "COPY --from=skopeo /usr/bin/skopeo /usr/local/lib/skopeo/skopeo.real"
        in dockerfile
    )
    assert "ARG TARGETARCH" in dockerfile
    assert "amd64) skopeo_loader=ld-linux-x86-64.so.2" in dockerfile
    assert "arm64) skopeo_loader=ld-linux-aarch64.so.1" in dockerfile
    assert "COPY --from=build /config /usr/local/lib/config" not in dockerfile
    assert "COPY --from=skopeo /skopeo-runtime/lib /usr/local/lib/skopeo" in dockerfile
    assert "/usr/lib64/ld-linux-*.so*" in dockerfile
    assert 'cp -aL "$library" /skopeo-runtime/lib/' in dockerfile
    assert "--library-path /usr/local/lib/skopeo" in dockerfile
    assert "skopeo.real" in dockerfile
    assert "patchelf" not in dockerfile
    assert "ldconfig" not in dockerfile
    assert "COPY --from=skopeo /etc/containers /etc/containers" in dockerfile
    assert "COPY --from=skopeo /etc/pki /etc/pki" not in dockerfile
    assert (
        "COPY --from=skopeo /usr/share/containers /usr/share/containers" in dockerfile
    )
    assert "/usr/bin/skopeo --version" in dockerfile
    assert "test ! -e /var/run/docker.sock" in dockerfile


def test_skopeo_image_stage_has_no_host_socket_or_privileged_runtime_contract() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM ${PYTHON_IMAGE} AS runtime-root", 1)[1]

    assert "--privileged" not in runtime
    assert "/var/run/docker.sock" in runtime
    assert "USER 10001:10001" in runtime
    assert "HOME=/tmp/control" in runtime
    assert "TMPDIR=/tmp/control" in runtime
    assert "XDG_CACHE_HOME=/tmp/control-cache" in runtime
    assert "/var/tmp" in runtime


def test_skopeo_is_used_only_where_images_enter_the_layered_store() -> None:
    source = ROOT / "control/src/vonk_control"
    store = (source / "oci_image_store.py").read_text(encoding="utf-8")
    preparation_modules = sorted((source / "runtime_image_preparation").rglob("*.py"))
    assert preparation_modules

    # The packaged binary is the one the store copies registry images with.
    assert 'skopeo: str = "/usr/bin/skopeo"' in store
    assert "--preserve-digests" in store
    # Preparation reads the layout directly and starts no process.
    for module in preparation_modules:
        preparation = module.read_text(encoding="utf-8")
        assert "import subprocess" not in preparation, module
        assert "from subprocess import" not in preparation, module
        assert "/usr/bin/skopeo" not in preparation, module


def test_controller_image_build_fails_when_the_admin_web_mount_path_is_empty() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    api = dockerfile.split("FROM runtime-root AS api-root", 1)[1].split("FROM ", 1)[0]

    assert "COPY --from=web /repo/control/web/dist ./src/vonk_control/web" in dockerfile
    assert (
        "from vonk_control.api.production import admin_web_root; "
        "assert (admin_web_root() / 'index.html').is_file()"
    ) in api


def test_python_tls_bundle_distrusts_compromised_emsign_root() -> None:
    # Wrong implementation: the old certifi lock still accepts chains to the compromised root.
    import ssl

    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    names = {
        value
        for certificate in context.get_ca_certs()
        for rdn in certificate["subject"]
        for key, value in rdn
        if key == "commonName"
    }
    assert "emSign ECC Root CA - G3" not in names
    assert (
        names
    )  # An empty trust store would hide the defect while breaking ordinary TLS.


@pytest.mark.built_image
@pytest.mark.parametrize("image_fixture", ["controller_image", "worker_image"])
def test_shipped_python_trust_store_is_present_and_distrusts_compromised_root(
    request: pytest.FixtureRequest, image_fixture: str
) -> None:
    # Wrong implementation: pip sees bootstrap certifi globally and omits it
    # from the prefix copied into runtime; checking the build interpreter passes.
    image: str = request.getfixturevalue(image_fixture)
    probe = """
import certifi, ssl
context = ssl.create_default_context(cafile=certifi.where())
roots = context.get_ca_certs()
assert roots
assert not any(value == "emSign ECC Root CA - G3"
               for cert in roots for rdn in cert["subject"]
               for key, value in rdn if key == "commonName")
"""
    completed = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
