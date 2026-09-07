from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-controller-skopeo"


def test_controller_skopeo_verification_is_digest_bound_and_rootless() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "abded792af33493f9aaa08132f564d3f9c12818193c0adc0a62f44ed84a04eda" in source
    assert "8ec3bf7d7bc514b7178db838b2e289ea5eca7bd065ce6a67b9f04cef3c85eba9" in source
    assert "source_reference" in source
    assert "@sha256:" in source
    assert "quay.io/skopeo/stable@sha256:e5d9c4af8ec327785c7ca938d1e4f8452c6a05014850e58e2ff9456899ebd97c" in source
    assert "--read-only" in source
    assert "--tmpfs /var/tmp:rw,nosuid,nodev,noexec,size=256m" in source
    assert "--user 10001:10001" in source
    assert "--privileged" not in source
    assert "docker.sock" not in source
    assert "source_child=$(skopeo inspect" in source
    assert "test \"$source_child\" = \"$expected_child\"" in source
    assert '"docker://${source_reference}"' in source
    assert '"docker://${SOURCE_REFERENCE}"' in source
    assert "oci-archive:/tmp/controller-skopeo.oci" in source
    assert "--tls-verify=true" in source
