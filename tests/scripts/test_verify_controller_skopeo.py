from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-controller-skopeo"


def test_controller_skopeo_verification_is_digest_bound_and_rootless() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "6c243579ceea16adaf6033c5626bce55a0bb1cfec50e973652306ee89aa8a753" in source
    assert "8fec5af224ef8590e9b20863d403c8e199efe7a577ae000e3ea1310dfed1d62e" in source
    assert "source_reference" in source
    assert "@sha256:" in source
    assert "quay.io/skopeo/stable@sha256:3af38d0df9fca73ae063a4f56eb58d548991655c506ffc3514e828a35cdb65ad" in source
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
