"""Exercise the combined Buildx response through the real image verifier."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _source_commit(tmp_path: Path) -> tuple[Path, str, str]:
    """A one-commit repository: its git dir, commit and tree."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "file").write_text("content\n")
    subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "source",
        ],
        check=True,
    )

    def rev(name: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", name], text=True
        ).strip()

    return repo / ".git", rev("HEAD"), rev("HEAD^{tree}")


@pytest.mark.parametrize("fault", ["transient", "legacy", "digest", "tree", "sbom"])
def test_published_image_fetch_recovery_still_verifies_identity_and_attestations(
    tmp_path, fault
):
    """An exact-source image must be bound to the verified commit's tree. Its
    revision label may name another commit with that tree; an image built
    before the tree label ("legacy") is bound through its commit's tree."""
    git_dir, commit, tree = _source_commit(tmp_path)
    revision = commit
    labels = {
        "org.opencontainers.image.source": "https://github.com/CarstVaartjes/vonk-forge",
        "org.opencontainers.image.revision": revision,
    }
    if fault != "legacy":
        labels["ai.vonkforge.source.tree"] = "1" * 40 if fault == "tree" else tree
    digest = "sha256:" + "b" * 64
    documents = json.loads(
        (ROOT / "tests/fixtures/buildx-inspection.json")
        .read_text()
        .replace("REVISION", revision)
        .replace("TREE", tree)
    )
    for image in documents["inspection"]["image"].values():
        image["config"]["Labels"] = labels
    if fault == "digest":
        documents["inspection"]["manifest"]["digest"] = "sha256:" + "0" * 64
    if fault == "sbom":
        documents["sbom"].pop("linux/arm64")
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    requests = tmp_path / "requests"
    # Stub only the remote reads. Execute the real shell, retry helper, jq
    # validation and multiarch verifier against their documented CLI responses.
    fake = f"""#!/usr/bin/env python3
import json, pathlib, sys
log = pathlib.Path({str(requests)!r})
args = sys.argv[1:]
key = " ".join(args)
previous = log.read_text().splitlines() if log.exists() else []
with log.open("a") as output: output.write(key + "\\n")
if {fault!r} == "transient" and key not in previous:
    print("partial response")
    print("connection reset by peer", file=sys.stderr)
    sys.exit(1)
print(json.dumps({documents!r}))
"""
    for name in ("docker", "skopeo"):
        tool = tool_bin / name
        tool.write_text(fake)
        tool.chmod(0o755)
    result = subprocess.run(
        [
            ROOT / "scripts/verify-published-image",
            "api",
            "ghcr.io/carstvaartjes/vonk-forge-api",
            digest,
            "dev-sha-" + commit,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{tool_bin}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GIT_DIR": str(git_dir),
        },
    )
    assert (result.returncode == 0) == (fault in ("transient", "legacy")), result.stderr
    calls = requests.read_text().splitlines()
    if fault == "transient":
        assert len(calls) == 2
    else:
        assert len(calls) == 1
    assert all("buildx imagetools inspect" in call for call in calls)
    if fault != "transient":
        assert len(calls) == len(set(calls))  # validation failures never retry
