"""Agent upgrades: package."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

import httpx2
from vonk_agent_protocol import (
    InvalidRequestReason,
    WaitReason,
)

from ..agent_upgrade_contract import (
    AgentUpgradePackage,
)
from ..bounded_retry import bounded_attempts
from ..categorized_errors import (
    InvalidType,
)

if TYPE_CHECKING:
    from .service import AgentUpgradeService

from .constants import _SHA256
from .errors import _RELEASE_PAUSES, AgentUpgradeRetryLater, AgentUpgradeUnavailable


class PackageMixin:
    def current_package(self) -> AgentUpgradePackage:
        """The signed current release; a channel that is mid-publication is asked
        again a few times before the requester is told to retry."""
        service = cast("AgentUpgradeService", self)

        refused: AgentUpgradeUnavailable | AgentUpgradeRetryLater | None = None
        for _attempt in bounded_attempts(_RELEASE_PAUSES):
            try:
                return service._current_package_once()
            except (AgentUpgradeUnavailable, AgentUpgradeRetryLater) as error:
                refused = error
        assert refused is not None
        raise refused

    def _current_package_once(self) -> AgentUpgradePackage:
        service = cast("AgentUpgradeService", self)
        prefix = f"/artifacts/{service._channel}"
        try:
            manifest_response = service._http.get(f"{prefix}/current.manifest")
            manifest_response.raise_for_status()
            if len(manifest_response.content) > 64 * 1024:
                raise AgentUpgradeUnavailable(
                    "agent release manifest is too large",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            manifest = dict(
                line.split("=", 1)
                for line in manifest_response.text.splitlines()
                if "=" in line
            )
            release_path = manifest.get("release_path", "")
            generation = manifest.get("generation", "")
            if (
                _SHA256.fullmatch(generation) is None
                or release_path
                != f"artifacts/{service._channel}/releases/{generation}/release.json"
            ):
                raise AgentUpgradeUnavailable(
                    "agent release manifest is invalid",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            release_response = service._http.get(f"/{release_path}")
            release_response.raise_for_status()
            if len(release_response.content) > 256 * 1024:
                raise AgentUpgradeUnavailable(
                    "agent release document is too large",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            release = release_response.json()
            artifact = release["artifacts"]["agent-package-linux-arm64"]
            signature_record = release["artifacts"][
                "agent-package-signature-linux-arm64"
            ]
            if not isinstance(artifact, Mapping) or not isinstance(
                signature_record, Mapping
            ):
                raise InvalidType(
                    "agent release artifact record is not an object",
                    reason=InvalidRequestReason.MALFORMED,
                )
            signature_response = service._http.get(f"/{signature_record['path']}")
            signature_response.raise_for_status()
            signature = signature_response.text.strip()
        except (httpx2.HTTPError, KeyError, TypeError, ValueError) as error:
            # Name what could not be resolved and what to do about it; the
            # operator cannot see the release relay from the CLI.
            raise AgentUpgradeUnavailable(
                f"the current {service._channel} agent package could not be resolved "
                f"from the release channel ({type(error).__name__}); check that the "
                "Controller reaches install.vonkforge.ai and that it serves a "
                "complete release, then retry",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        if (
            release.get("channel") != service._channel
            or release.get("generation") != generation
            or artifact.get("host_signature") != signature
            or signature_record.get("sha256")
            != hashlib.sha256(signature_response.content).hexdigest()
            or signature_record.get("size") != len(signature_response.content)
        ):
            raise AgentUpgradeRetryLater("current agent release is inconsistent")
        return service._package(
            {
                "architecture": artifact.get("architecture"),
                "package_bytes": artifact.get("size"),
                "package_sha256": artifact.get("sha256"),
                "package_signature": signature,
                "package_url": f"https://install.vonkforge.ai/{artifact.get('path')}",
                "package_version": artifact.get("package_version"),
                "schema_version": 1,
                "target_binary_digest": artifact.get("target_binary_digest"),
                "target_build_digest": artifact.get("target_build_digest"),
            }
        )
