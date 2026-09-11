from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-controller-skopeo"


def test_controller_skopeo_verification_is_digest_bound_and_rootless() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "0e392474a4383b733038b85eff26ade929d2ff10e8deead25a6add3ed79fb362" in source
    assert "807f42a95c0f05f397eb505b577b6de49048b865c4e29146d1231324c27e1e59" in source
    assert "source_reference" in source
    assert "@sha256:" in source
    assert (
        "quay.io/skopeo/stable:v1.22.2-immutable@sha256:"
        "4a16d57b37617a04b3d643079a477a2848efe892dffcdf0ce56df4262b65f810" in source
    )
    assert "--read-only" in source
    assert "--tmpfs /var/tmp:rw,nosuid,nodev,noexec,size=256m" in source
    assert "--user 10001:10001" in source
    assert "--privileged" not in source
    assert "docker.sock" not in source
    assert "source_child=$(skopeo inspect" in source
    assert "test \"$source_child\" = \"$expected_child\"" in source
    # skopeo rejects a reference carrying both a tag and a digest, so the
    # reviewed source is reduced to bare repository plus index digest.
    assert 'skopeo_source="quay.io/skopeo/stable@${source_reference##*@}"' in source
    assert '"docker://${skopeo_source}"' in source
    assert '"docker://${SOURCE_REFERENCE}"' in source
    assert "oci-archive:/tmp/controller-skopeo.oci" in source
    assert "--tls-verify=true" in source
