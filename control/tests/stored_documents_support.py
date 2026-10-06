"""Valid stored documents for rows a test inserts only to satisfy a reference."""

from __future__ import annotations

from vonk_control.recipe_builds import BUILD_ARTIFACT_FORMAT


def valid_policy_report(passed: bool = True) -> dict[str, object]:
    """A build policy report that satisfies its stored-document contract."""

    return {
        "passed": passed,
        "source_bundle_sha256": "a" * 64,
        "dockerfile": "Dockerfile",
        "findings": [],
        "builder_binary_digest": None,
        "artifact_format": BUILD_ARTIFACT_FORMAT,
    }
