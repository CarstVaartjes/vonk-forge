from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/recipe-source-bundle"


def test_source_bundle_cli_is_deterministic_and_emits_canonical_manifest(
    tmp_path: Path,
) -> None:
    assert SCRIPT.is_file()
    context = tmp_path / "context"
    context.mkdir()
    (context / "z.txt").write_text("z\n", encoding="utf-8")
    (context / "a.txt").write_text("a\n", encoding="utf-8")
    first = tmp_path / "first"
    second = tmp_path / "second"

    one = subprocess.run(
        [str(SCRIPT), str(context), "--output-dir", str(first)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    two = subprocess.run(
        [str(SCRIPT), str(context), "--output-dir", str(second)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    first_result = json.loads(one.stdout)
    second_result = json.loads(two.stdout)

    assert first_result == second_result
    assert first_result["source_sha256"] == hashlib.sha256(
        (first / "manifest.json").read_bytes()
    ).hexdigest()
    first_archive = first / f"{first_result['source_sha256']}.tar"
    second_archive = second / f"{second_result['source_sha256']}.tar"
    assert first_archive.read_bytes() == second_archive.read_bytes()
    with tarfile.open(first_archive) as archive:
        assert archive.getnames() == ["a.txt", "z.txt"]
        assert all(member.uid == member.gid == member.mtime == 0 for member in archive)
        assert all(member.mode == 0o644 for member in archive)


def test_source_bundle_cli_rejects_symlinks(tmp_path: Path) -> None:
    assert SCRIPT.is_file()
    context = tmp_path / "context"
    context.mkdir()
    (context / "file").write_text("data", encoding="utf-8")
    (context / "link").symlink_to("file")

    result = subprocess.run(
        [str(SCRIPT), str(context), "--output-dir", str(tmp_path / "out")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
