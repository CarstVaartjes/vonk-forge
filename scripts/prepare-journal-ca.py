#!/usr/bin/env python3
"""Immutable Smallstep source verification for hosted compilation."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CA = ROOT / "deploy/compose/step-ca"


def main() -> None:
    lock = json.loads((CA / "source.lock.json").read_text())
    manifest = json.loads((CA / "upstream/manifest.json").read_text())
    if lock["upstream_commit"] != manifest["upstream_commit"]:
        raise ValueError("CA source and patch revisions differ")
    expected_url = (
        "https://codeload.github.com/smallstep/certificates/tar.gz/"
        + lock["upstream_commit"]
    )
    if lock["archive_url"] != expected_url:
        raise ValueError("CA archive is outside the pinned upstream")
    patch = CA / "upstream/context-signing.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise ValueError("CA context patch digest differs")
    destination = CA / "upstream/certificates"
    if destination.exists():
        raise ValueError("CA generated source destination already exists")
    with urllib.request.urlopen(expected_url, timeout=60) as response:
        archive = response.read(32 * 1024 * 1024 + 1)
    if len(archive) > 32 * 1024 * 1024:
        raise ValueError("CA source archive exceeds 32 MiB input bound")
    if hashlib.sha256(archive).hexdigest() != lock["archive_sha256"]:
        raise ValueError("CA source archive digest differs")
    destination.mkdir(exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        prefix = f"certificates-{lock['upstream_commit']}/"
        members = []
        for member in bundle.getmembers():
            if member.name == prefix.rstrip("/"):
                continue
            if not member.name.startswith(prefix):
                raise ValueError("CA source archive root differs")
            member.name = member.name.removeprefix(prefix)
            members.append(member)
        bundle.extractall(destination, members=members, filter="data")
    for relative, expected_digest in manifest["source_sha256"].items():
        source = destination / relative
        if not source.resolve().is_relative_to(destination.resolve()):
            raise ValueError("CA manifest path escapes source root")
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected_digest:
            raise ValueError(f"CA patch input digest differs: {relative}")
    subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=destination, check=True, timeout=30
    )
    subprocess.run(
        ["git", "apply", str(patch)], cwd=destination, check=True, timeout=30
    )


if __name__ == "__main__":
    main()
