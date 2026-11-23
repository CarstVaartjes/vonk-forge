from __future__ import annotations

import hashlib
import json
import tarfile
from collections.abc import Mapping
from pathlib import PurePosixPath

from vonk_forge_contracts import (
    RecipeDefinition,
)

# Flat names are at most 128 characters and real extended headers are under 1 KiB.
MAX_TAR_EXTENDED_HEADER_BYTES = 8 * 1024


_EXTENDED_HEADER_TYPES = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)


class _BoundedTarInfo(tarfile.TarInfo):
    """Refuse an oversized extended header before CPython reads it into memory.

    tarfile reads a whole PAX/GNU extended header into memory while parsing the
    header, before any check of ours (and before the signature) can run.
    `_proc_member` is the one private hook that sees the declared size first;
    it has kept this shape since Python 2, and a rename fails the tests loudly.
    """

    def _proc_member(self, tar: tarfile.TarFile) -> tarfile.TarInfo | None:
        if (
            self.type in _EXTENDED_HEADER_TYPES
            and self.size > MAX_TAR_EXTENDED_HEADER_BYTES
        ):
            raise tarfile.ReadError("extended tar header exceeds its bound")
        return super()._proc_member(tar)  # type: ignore[no-any-return,attr-defined]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(value: str) -> bool:
    if not value or "\0" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and value == path.as_posix()
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _json(raw: bytes) -> object:
    return json.loads(raw)


def _validate_package_paths(
    recipe: RecipeDefinition, package_paths: set[str], build_inputs: object
) -> None:
    """Validate source and fixture closure using BuildContext as a prefix."""
    build = recipe.execution.build
    context = build.context.path.rstrip("/")
    if not any(
        path == context or path.startswith(f"{context}/") for path in package_paths
    ):
        raise ValueError("build context is missing from package")
    required = {build.dockerfile, *(patch.path for patch in build.patches)}
    missing = sorted(required - package_paths)
    if missing:
        raise ValueError(f"build package files are missing: {', '.join(missing)}")
    if not isinstance(build_inputs, list) or not any(
        isinstance(value, Mapping)
        and value.get("kind") == "oci-image"
        and isinstance(value.get("reference"), str)
        and value["reference"].endswith(f"@sha256:{build.base_image.digest}")
        for value in build_inputs
    ):
        raise ValueError("build base image digest is not in package inputs")
    for check in recipe.validation.serving.checks:
        missing = sorted(set(check.fixture_paths()) - package_paths)
        if missing:
            raise ValueError(f"serving package files are missing: {', '.join(missing)}")
