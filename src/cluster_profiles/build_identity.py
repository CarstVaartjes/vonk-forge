"""Read the identity embedded in a built CLI wheel, including offline."""

from __future__ import annotations

import json
from importlib import metadata, resources

from .runtime_identity import packaged_runtime_identity


def current_build() -> dict[str, object]:
    try:
        version = metadata.version("vonk-cluster-profiles")
    except metadata.PackageNotFoundError:
        version = "0.1.1"
    try:
        value = json.loads(
            resources.files("cluster_profiles")
            .joinpath("build-identity.json")
            .read_text()
        )
    except (OSError, ValueError):
        value = {}
    source_sha = packaged_runtime_identity().source_sha
    release_version = value.get("release_version") if isinstance(value, dict) else None
    if isinstance(release_version, str) and release_version:
        version = release_version
    return {"version": version, "source_sha": source_sha}
