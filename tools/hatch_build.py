"""Generate CLI consumers and stamp source identity into the wheel."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import override

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        root = Path(self.root)
        if self.target_name != "wheel" or version == "editable":
            return
        self._build_directory = tempfile.TemporaryDirectory(prefix="vonkctl-build-")
        directory = Path(self._build_directory.name)
        subprocess.run(
            [
                str(root / "scripts/generate-control-clients"),
                "--python-only",
                "--output-root",
                str(directory),
            ],
            cwd=root,
            check=True,
            timeout=1200,
        )
        subprocess.run(
            [
                str(root / "scripts/export-agent-wire-schema"),
                "--output",
                str(directory / "wire.json"),
            ],
            cwd=root,
            check=True,
            timeout=1200,
        )
        include = build_data.setdefault("force_include", {})
        assert isinstance(include, dict)
        include[
            str(directory / "src/cluster_profiles/schemas/control-openapi.json")
        ] = "cluster_profiles/schemas/control-openapi.json"
        include[str(directory / "src/cluster_profiles/generated_control")] = (
            "cluster_profiles/generated_control"
        )

        identity = directory / "build-identity.json"
        subprocess.run(
            [
                sys.executable,
                str(root / "tools/stamp_build_identity.py"),
                "--control-openapi",
                str(directory / "src/cluster_profiles/schemas/control-openapi.json"),
                "--wire",
                str(directory / "wire.json"),
                "--output",
                str(identity),
            ],
            check=True,
            timeout=60,
        )
        include[str(identity)] = "cluster_profiles/build-identity.json"

    @override
    def finalize(
        self, version: str, build_data: dict[str, object], artifact_path: str
    ) -> None:
        directory = getattr(self, "_build_directory", None)
        if directory is not None:
            directory.cleanup()
