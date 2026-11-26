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
    tmp_path: Path, channel: str, ref: str, content: bytes = b"accepted bundle bytes"
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
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("channel", "ref"), [("dev", "refs/heads/main"), ("stable", "refs/tags/v1.0.0")]
)
def test_bundle_is_stored_by_source_and_the_pointer_written_last(
    tmp_path: Path, channel: str, ref: str
) -> None:
    result = _publish(tmp_path, channel, ref)
    assert result.returncode == 0, result.stderr
    name = f"recipe-tools-{SOURCE}.tar.gz"
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
        "sha256": hashlib.sha256(b"accepted bundle bytes").hexdigest(),
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


def test_a_rerun_republishes_and_other_bytes_for_one_source_are_refused(
    tmp_path: Path,
) -> None:
    assert _publish(tmp_path, "dev", "refs/heads/main").returncode == 0
    assert _publish(tmp_path, "dev", "refs/heads/main").returncode == 0
    assert _publish(tmp_path, "dev", "refs/heads/main", b"other bytes").returncode == 2
