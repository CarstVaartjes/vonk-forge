from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-controller-skopeo"


def test_controller_skopeo_verification_is_digest_bound_and_rootless() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "9f6b762ce968b90b509c3bfa58f3d7abdbd1e0789340989db1572a1156fef116" in source
    assert "75ab1f75f046d1597502a1ef838000d8462bfdf32976f2966658728a76853a49" in source
    assert "source_reference" in source
    assert "@sha256:" in source
    assert "quay.io/skopeo/stable@sha256:8d25aabcf965e267b6a6ad02ff8da5512f77de1490063625093ff564797e88bc" in source
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
