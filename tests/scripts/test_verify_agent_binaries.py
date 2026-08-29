import json
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-agent-binaries"
BUILD_DIGEST = "sha256:" + "a" * 64
SEMANTIC_VERSION = "0.1.0"


def binary(machine: int, *, identity: bool) -> bytes:
    raw = bytearray(512)
    raw[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", raw, 18, machine)
    if identity:
        markers = (
            f"VONK_AGENT_BUILD_DIGEST={BUILD_DIGEST}".encode(),
            f"VONK_AGENT_SEMANTIC_VERSION={SEMANTIC_VERSION}".encode(),
        )
        raw[128 : 128 + len(markers[0])] = markers[0]
        raw[256 : 256 + len(markers[1])] = markers[1]
    return bytes(raw)


def fixture(root: Path, machine: int = 183) -> Path:
    root.mkdir()
    (root / "vonk-agent").write_bytes(binary(machine, identity=True))
    (root / "vonk-agent-helper").write_bytes(binary(machine, identity=False))
    for path in root.iterdir():
        path.chmod(0o555)
    return root


def verify(
    root: Path, architecture: str = "linux-arm64"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            SCRIPT,
            "--architecture",
            architecture,
            "--semantic-version",
            SEMANTIC_VERSION,
            "--build-digest",
            BUILD_DIGEST,
            "--binaries-dir",
            root,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_verifier_accepts_exact_identity_bound_binary_set(tmp_path: Path) -> None:
    root = fixture(tmp_path / "binaries")

    result = verify(root)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["architecture"] == "linux-arm64"
    assert evidence["build_digest"] == BUILD_DIGEST
    assert evidence["semantic_version"] == SEMANTIC_VERSION
    assert set(evidence["files"]) == {"vonk-agent", "vonk-agent-helper"}


@pytest.mark.parametrize("mutation", ("architecture", "identity", "extra", "symlink"))
def test_verifier_rejects_unsafe_or_unbound_binary_sets(
    tmp_path: Path, mutation: str
) -> None:
    root = fixture(tmp_path / "binaries")
    if mutation == "architecture":
        (root / "vonk-agent-helper").chmod(0o644)
        (root / "vonk-agent-helper").write_bytes(binary(62, identity=False))
        (root / "vonk-agent-helper").chmod(0o555)
    elif mutation == "identity":
        (root / "vonk-agent").chmod(0o644)
        (root / "vonk-agent").write_bytes(binary(183, identity=False))
        (root / "vonk-agent").chmod(0o555)
    elif mutation == "extra":
        (root / "unexpected").write_text("no")
    else:
        target = root / "vonk-agent-helper"
        target.unlink()
        target.symlink_to(root / "vonk-agent")

    result = verify(root)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("verify-agent-binaries: ")
