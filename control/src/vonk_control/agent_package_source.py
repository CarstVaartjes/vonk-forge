"""Exact published source package resolution; no version or current-release fallback."""
import httpx
from vonk_agent_protocol.package_source import AgentPackageSource


def load_package_source(client: httpx.Client, channel: str, build_digest: str, binary_digest: str) -> AgentPackageSource:
    import re
    if channel not in {"dev", "stable"} or re.fullmatch(r"sha256:[0-9a-f]{64}", build_digest) is None or re.fullmatch(r"[0-9a-f]{64}", binary_digest) is None:
        raise ValueError("installed agent identity is invalid")
    response = client.get(f"/artifacts/{channel}/agent-builds/{build_digest[7:]}/{binary_digest}/package.json")
    response.raise_for_status()
    if len(response.content) > 16384:
        raise ValueError("source package metadata is too large")
    source = AgentPackageSource.model_validate_json(response.content)
    if source.build_digest != build_digest or source.package.binary_sha256 != binary_digest:
        raise ValueError("source package does not match installed identity")
    return source
