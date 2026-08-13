#!/usr/bin/env python3
"""Resolve only the artifact selected in the signed Vonk runtime contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPOSITORY = "deepseek-ai/DeepSeek-V4-Flash-0731"
REVISION = "9e165c30e2704aec5d9d593cce3eebd58bbef1cb"


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    contract_path, root_path = map(Path, sys.argv[1:])
    try:
        document = json.loads(contract_path.read_text(encoding="utf-8"))
        artifacts = document["artifacts"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        return 1
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        return 1
    artifact = artifacts[0]
    expected = {
        "kind": "huggingface.snapshot",
        "repository": REPOSITORY,
        "revision": REVISION,
    }
    if not isinstance(artifact, dict) or any(
        artifact.get(key) != value for key, value in expected.items()
    ):
        return 1
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str):
        return 1
    model = Path(raw_path)
    try:
        if (
            not model.is_absolute()
            or model.is_symlink()
            or model.parent != root_path / "sha256"
        ):
            return 1
        if not (model / "config.json").is_file():
            return 1
        if not (model / "encoding/encoding_dsv4.py").is_file():
            return 1
    except OSError:
        return 1
    print(model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
