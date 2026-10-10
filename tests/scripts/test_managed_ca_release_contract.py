"""Managed CA publication must close the installer graph before signing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from vonk_agent_protocol.installer_release import InstallerReleaseImages


def test_installer_image_graph_requires_managed_ca_digest() -> None:
    """Catches signing an installer that can silently retain the third-party CA."""
    images = {
        role: f"ghcr.io/carstvaartjes/vonk-forge-{role}:dev-sha-{'a' * 40}@sha256:{'b' * 64}"
        for role in ("api", "worker", "hermes", "litellm", "ca")
    }
    assert InstallerReleaseImages.model_validate(images).ca == images["ca"]
    expected = images["ca"]
    del images["ca"]
    with pytest.raises(ValidationError):
        InstallerReleaseImages.model_validate(images)
    images["ca"] = "ghcr.io/carstvaartjes/vonk-forge-ca:dev"
    with pytest.raises(ValidationError):
        InstallerReleaseImages.model_validate(images)
    images["ca"] = expected
    accepted = InstallerReleaseImages.model_validate(images)
    restored = InstallerReleaseImages.model_validate_json(accepted.model_dump_json())
    assert restored == accepted
