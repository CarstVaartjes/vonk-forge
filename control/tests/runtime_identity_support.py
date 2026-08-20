from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vonk_control.agent_jobs import AgentJobService

PACKAGED_RUNTIME_IDENTITY = {
    "architecture": "linux-amd64",
    "binary_digest": "c" * 64,
    "build_digest": "sha256:" + "b" * 64,
    "semantic_version": "1.2.3",
    "self_test_passed": True,
}
_DEFAULT_IDENTITY = object()


def claim_agent(
    service: AgentJobService,
    *args: Any,
    runtime_identity: Mapping[str, object] | None | object = _DEFAULT_IDENTITY,
    **kwargs: Any,
):
    """Claim through the exact packaged Rust runtime contract."""
    if runtime_identity is _DEFAULT_IDENTITY:
        runtime_identity = PACKAGED_RUNTIME_IDENTITY
    return service.claim(
        *args,
        runtime_identity=runtime_identity,
        **kwargs,
    )
