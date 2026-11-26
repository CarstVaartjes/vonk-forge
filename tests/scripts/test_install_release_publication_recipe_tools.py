from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/release/install-release-publication"
SOURCE = "a" * 40


def _publish(
    tmp_path: Path,
    channel: str,
    ref: str,
    content: bytes = b"accepted bundle bytes",
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    bundle = tmp_path / "recipe-tools.tar.gz"
    bundle.write_bytes(content)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-recipe-tools",
            "--bundle-file",
            str(bundle),
            "--channel",
            channel,
            "--source-sha",
            SOURCE,
            "--ref",
            ref,
            "--filesystem",
            str(tmp_path / "store"),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("channel", "ref"), [("dev", "refs/heads/main"), ("stable", "refs/tags/v1.0.0")]
)
def test_bundle_is_stored_by_its_digest_and_the_pointer_written_last(
    tmp_path: Path, channel: str, ref: str
) -> None:
    result = _publish(tmp_path, channel, ref)
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(b"accepted bundle bytes").hexdigest()
    name = f"recipe-tools-{digest}.tar.gz"
    written = [json.loads(line) for line in result.stdout.splitlines()]
    assert written == [
        {"key": f"artifacts/{channel}/{name}", "phase": "bundle"},
        {"key": f"artifacts/{channel}/recipe-tools.json", "phase": "pointer"},
    ]
    store = tmp_path / "store/artifacts" / channel
    assert (store / name).read_bytes() == b"accepted bundle bytes"
    assert json.loads((store / "recipe-tools.json").read_text()) == {
        "bundle": name,
        "ref": ref,
        "sha256": digest,
        "source_sha": SOURCE,
    }


@pytest.mark.parametrize(
    ("channel", "ref"),
    [
        ("dev", "refs/tags/v1.0.0"),
        ("stable", "refs/heads/main"),
        ("stable", "refs/tags/v1.*"),
    ],
)
def test_a_ref_outside_its_channel_publishes_nothing(
    tmp_path: Path, channel: str, ref: str
) -> None:
    result = _publish(tmp_path, channel, ref)
    assert result.returncode == 2
    assert not (tmp_path / "store").exists()


def test_a_rerun_republishes_and_a_rebuild_never_collides(tmp_path: Path) -> None:
    assert _publish(tmp_path, "dev", "refs/heads/main").returncode == 0
    assert _publish(tmp_path, "dev", "refs/heads/main").returncode == 0
    assert _publish(tmp_path, "dev", "refs/heads/main", b"other bytes").returncode == 0
    store = tmp_path / "store/artifacts/dev"
    pointer = json.loads((store / "recipe-tools.json").read_text())
    assert (store / pointer["bundle"]).read_bytes() == b"other bytes"
    assert len(list(store.glob("recipe-tools-*.tar.gz"))) == 2


def test_bundle_only_writes_the_object_and_no_pointer(tmp_path: Path) -> None:
    result = _publish(
        tmp_path, "dev", "refs/heads/main", b"accepted bundle bytes", "--bundle-only"
    )
    assert result.returncode == 0, result.stderr
    store = tmp_path / "store/artifacts/dev"
    assert [path.name for path in store.iterdir()] == [
        f"recipe-tools-{hashlib.sha256(b'accepted bundle bytes').hexdigest()}.tar.gz"
    ]
