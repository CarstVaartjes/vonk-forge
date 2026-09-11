from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-controller-skopeo"


def test_controller_skopeo_verification_is_digest_bound_and_rootless() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ab6d8bc7b5985581d69d143e7e57606bcee6796757b9147bcc37f1df6b73d820" in source
    assert "2dada7d8c2bf3cb87677da0ca57eba620394eaa4a0827f6b21cffeedbf3a68ea" in source
    assert "source_reference" in source
    assert "@sha256:" in source
    assert "quay.io/skopeo/stable@sha256:545723edab7793112a5c8fc36963f5cad43c6f27c0bda63c5fbc8b5d4d336036" in source
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
