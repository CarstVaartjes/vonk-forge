"""Stamp the source identity into a CLI wheel without changing the worktree."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        source_sha = os.environ.get("VONK_BUILD_SOURCE_SHA") or None
        release_version = os.environ.get("VONK_BUILD_RELEASE_VERSION")
        if source_sha is not None and re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
            raise ValueError("VONK_BUILD_SOURCE_SHA must be a 40-character source SHA")
        if (
            release_version is not None
            and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+:~-]{0,127}", release_version)
            is None
        ):
            raise ValueError("VONK_BUILD_RELEASE_VERSION is invalid")
        root = Path(self.root)

        def fingerprint(path: Path) -> str:
            document = json.loads(path.read_text())
            return hashlib.sha256(
                json.dumps(
                    document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()

        control_fingerprint = fingerprint(
            root / "src/cluster_profiles/schemas/control-openapi.json"
        )
        worker_fingerprint = fingerprint(
            root / "rust/crates/vonk-agent-protocol/schema/wire.json"
        )
        directory = Path(tempfile.mkdtemp(prefix="vonkctl-build-"))
        identity = directory / "build-identity.json"
        identity.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "control_contract_sha256": control_fingerprint,
                    "worker_contract_sha256": worker_fingerprint,
                    "source_sha": source_sha,
                    "release_version": release_version,
                },
                sort_keys=True,
            )
            + "\n"
        )
        self._identity_directory = directory
        include = build_data.setdefault("force_include", {})
        assert isinstance(include, dict)
        include[str(identity)] = "cluster_profiles/build-identity.json"

    def finalize(
        self, version: str, build_data: dict[str, object], artifact_path: str
    ) -> None:
        directory = getattr(self, "_identity_directory", None)
        if directory is not None:
            (directory / "build-identity.json").unlink(missing_ok=True)
            directory.rmdir()
