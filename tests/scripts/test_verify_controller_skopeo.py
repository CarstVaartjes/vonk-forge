from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-controller-skopeo"


def test_controller_skopeo_verification_is_digest_bound_and_rootless() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ab4c269c9e2bd11affe2666b860fb651a15afec121c15986b052b02e09d86239" in source
    assert "916612c4c9bcf1dd633137ec4ce88987e368c16b3d611642006ca5996fe28c98" in source
    assert "source_reference" in source
    assert "@sha256:" in source
    assert (
        "quay.io/skopeo/stable:v1.22.3-immutable@sha256:"
        "c0ee1f4edca5c01cb8d5611124f92f3cc47196ecab68aee5d0f90834e00574d5" in source
    )
    assert "--read-only" in source
    assert "--tmpfs /var/tmp:rw,nosuid,nodev,noexec,size=256m" in source
    assert "--user 10001:10001" in source
    assert "--privileged" not in source
    assert "docker.sock" not in source
    assert "source_child=$(skopeo inspect" in source
    assert 'test "$source_child" = "$expected_child"' in source
    # skopeo rejects a reference carrying both a tag and a digest, so the
    # reviewed source is reduced to bare repository plus index digest.
    assert 'skopeo_source="quay.io/skopeo/stable@${source_reference##*@}"' in source
    assert '"docker://${skopeo_source}"' in source
    assert '"docker://${SOURCE_REFERENCE}"' in source
    assert "oci-archive:/tmp/controller-skopeo.oci" in source
    assert "--tls-verify=true" in source
