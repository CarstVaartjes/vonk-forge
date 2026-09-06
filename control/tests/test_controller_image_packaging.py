from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "control/Dockerfile"

SKOPEO_INDEX = "sha256:8d25aabcf965e267b6a6ad02ff8da5512f77de1490063625093ff564797e88bc"
SKOPEO_AMD64 = "sha256:9f6b762ce968b90b509c3bfa58f3d7abdbd1e0789340989db1572a1156fef116"
SKOPEO_ARM64 = "sha256:75ab1f75f046d1597502a1ef838000d8462bfdf32976f2966658728a76853a49"


def test_controller_image_pins_and_packages_the_reviewed_skopeo_transport() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert f"ARG SKOPEO_IMAGE=quay.io/skopeo/stable@{SKOPEO_INDEX}" in dockerfile
    assert f"ARG SKOPEO_INDEX_DIGEST={SKOPEO_INDEX}" in dockerfile
    assert f"ARG SKOPEO_AMD64_DIGEST={SKOPEO_AMD64}" in dockerfile
    assert f"ARG SKOPEO_ARM64_DIGEST={SKOPEO_ARM64}" in dockerfile
    assert "FROM ${SKOPEO_IMAGE} AS skopeo" in dockerfile
    assert "ARG TARGETARCH" in dockerfile
    assert "skopeo inspect --tls-verify=true --raw" in dockerfile
    assert "architecture" in dockerfile
    assert '"$TARGETARCH"' in dockerfile
    assert "expected_child=\"$SKOPEO_ARM64_DIGEST\"" in dockerfile
    assert "COPY --from=skopeo /usr/bin/skopeo /usr/bin/skopeo" in dockerfile
    assert "COPY --from=build /config /usr/local/lib/config" in dockerfile
    assert "COPY --from=skopeo /skopeo-runtime/lib /usr/local/lib/skopeo" in dockerfile
    assert "ldconfig" in dockerfile
    assert "COPY --from=skopeo /etc/containers /etc/containers" in dockerfile
    assert "COPY --from=skopeo /etc/pki /etc/pki" in dockerfile
    assert "COPY --from=skopeo /etc/ssl /etc/ssl" in dockerfile
    assert "COPY --from=skopeo /usr/share/containers /usr/share/containers" in dockerfile
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


def test_skopeo_production_transport_uses_real_inspect_copy_and_archive_commands() -> None:
    source = (ROOT / "control/src/vonk_control/runtime_image_preparation.py").read_text(
        encoding="utf-8"
    )
    assert 'executable: str = "/usr/bin/skopeo"' in source
    assert '"inspect"' in source
    assert '"copy"' in source
    assert 'f"oci-archive:{destination}"' in source
    assert 'f"oci-archive:{archive}"' in source
    assert "docker://{reference}" in source
    assert "--override-arch" in source
    assert "--override-os" in source


def test_skopeo_digest_and_platform_arguments_are_bounded() -> None:
    source = (ROOT / "control/src/vonk_control/runtime_image_preparation.py").read_text(
        encoding="utf-8"
    )
    assert re.search(r"def _platform_args\(architecture: str\).*?override-arch", source, re.DOTALL)
    assert "expected_manifest" in source
    assert "runtime_image.digest_mismatch" in source
    assert "runtime_image.architecture_mismatch" in source
