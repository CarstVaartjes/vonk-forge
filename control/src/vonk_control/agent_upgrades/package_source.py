"""Exact published source package resolution; no version or current-release fallback."""

import re

import httpx2

from vonk_agent_protocol.package_source import AgentPackageSource


def load_package_source(
    client: httpx2.Client, channel: str, build_digest: str, binary_digest: str
) -> AgentPackageSource:

    if (
        channel not in {"dev", "stable"}
        or re.fullmatch(r"sha256:[0-9a-f]{64}", build_digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", binary_digest) is None
    ):
        raise ValueError("installed agent identity is invalid")
    # The installed package is identified by its digests; the channel is only
    # where it was published: look in the Controller's channel, then the other.
    try:
        return _read_source(client, channel, build_digest, binary_digest)
    except httpx2.HTTPError, ValueError:
        other = "stable" if channel == "dev" else "dev"
        return _read_source(client, other, build_digest, binary_digest)


def _read_source(
    client: httpx2.Client, channel: str, build_digest: str, binary_digest: str
) -> AgentPackageSource:
    response = client.get(
        f"/artifacts/{channel}/agent-builds/{build_digest[7:]}/{binary_digest}/package.json"
    )
    response.raise_for_status()
    if len(response.content) > 16384:
        raise ValueError("source package metadata is too large")
    source = AgentPackageSource.model_validate_json(response.content)
    if (
        source.build_digest != build_digest
        or source.package.binary_sha256 != binary_digest
    ):
        raise ValueError("source package does not match installed identity")
    return source
