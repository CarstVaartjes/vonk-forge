"""Secret-free operator diagnostics for controller-managed agent upgrades."""

from __future__ import annotations

from collections.abc import Mapping

#: The agent's recorded body for the by-design handoff after the signed helper
#: accepted the package: the agent cannot observe its own restart, so the
#: Controller completes the upgrade only from a later authenticated claim that
#: proves the exact running identity.  The name is owned by
#: ``vonk_agent::agent_upgrade::UPGRADE_AWAITING_IDENTITY_REASON``; this is the
#: Controller's single copy of the wire string, so the recoverable set, the
#: retry set and the operator surface cannot drift from each other.  It names
#: the awaiting state, never a failed install.
AGENT_UPGRADE_AWAITING_IDENTITY_REASON = (
    "agent upgrade installed the package; awaiting identity confirmation"
)

#: The same by-design handoff as spelled by every agent package built before the
#: rename above.  A Controller is deployed *ahead* of the agents it upgrades, so
#: during a one-at-a-time rollout the handoff arrives from a peer that still
#: spells it the old way.  That makes this a live agent's wire value, not a
#: retired one: dropping it made a completed install read as a hard failure.
#: Observed live on 2026-09-17, `vonkctl fleet upgrade --all --strategy
#: one-at-a-time` recorded `failed` / `target_proven: false` /
#: `reason: agent upgrade did not restart the service` for both Sparks while
#: they were already running the exact target binary, and the operator could not
#: tell a successful handoff from a real failure.  Remove this spelling once no
#: deployable agent package predates the rename.
AGENT_UPGRADE_AWAITING_IDENTITY_PREDECESSOR_REASON = (
    "agent upgrade did not restart the service"
)

#: Every body that means "the signed helper accepted the package; prove the
#: running identity from a later authenticated claim".  One set, so the
#: recoverable reasons and the automatic-retry exclusion cannot drift apart.
AGENT_UPGRADE_AWAITING_IDENTITY_REASONS = frozenset(
    {
        AGENT_UPGRADE_AWAITING_IDENTITY_REASON,
        AGENT_UPGRADE_AWAITING_IDENTITY_PREDECESSOR_REASON,
    }
)

GENERIC_AGENT_UPGRADE_REASONS = frozenset(
    {
        "agent upgrade request is invalid",
        "agent upgrade helper rejected the request",
        "agent upgrade helper rejected the request: operation_failed",
    }
)

RECOVERABLE_AGENT_UPGRADE_REASONS = frozenset(
    {
        *GENERIC_AGENT_UPGRADE_REASONS,
        "agent upgrade helper is unavailable",
        *AGENT_UPGRADE_AWAITING_IDENTITY_REASONS,
        "agent upgrade helper rejected the request: package_preflight_failed",
        "agent upgrade helper rejected the request: package_verification_failed",
        "agent upgrade helper rejected the request: package_metadata_failed",
        "agent upgrade helper rejected the request: package_custody_failed",
        "agent upgrade helper rejected the request: package_install_failed",
    }
)


def agent_upgrade_next_action(*, retry_queued: bool) -> str:
    if retry_queued:
        return (
            "Wait for the controller-managed retry behind its safety delay; it will "
            "not dispatch before the reported retry time. Do not manually resume "
            "the rollout again."
        )
    return (
        "Keep the rollout paused and inspect the Spark package-helper and dpkg "
        "recovery state before resuming. When ready, Resume queues the retry behind "
        "a new safety delay; it does not dispatch immediately. Do not advance to "
        "another Spark until this Spark reports the exact target identity."
    )


def operator_agent_upgrade_reason(
    *,
    node_id: str,
    attempt_count: int,
    package: Mapping[str, object],
    observed_semantic_version: str | None,
    observed_binary_digest: str | None,
    observed_build_digest: str | None,
    raw_reason: str,
    retry_queued: bool,
) -> str:
    """Explain a current helper failure that omits its failed stage."""

    if raw_reason not in GENERIC_AGENT_UPGRADE_REASONS:
        return raw_reason
    observed = _identity_label(
        version=observed_semantic_version,
        binary_digest=observed_binary_digest,
        build_digest=observed_build_digest,
    )
    expected = _identity_label(
        version=_string(package.get("package_version")),
        binary_digest=_string(package.get("target_binary_digest")),
        build_digest=_string(package.get("target_build_digest")),
    )
    attempt_label = "attempt" if attempt_count == 1 else "attempts"
    return (
        f"Spark {node_id} still reports {observed} after {attempt_count} install "
        f"{attempt_label}; target {expected} was not proven. The helper "
        "returned a generic failure that does not distinguish package installation "
        "from service restart failure and does not establish an authorization or "
        f"download failure. {agent_upgrade_next_action(retry_queued=retry_queued)}"
    )


def _identity_label(
    *,
    version: str | None,
    binary_digest: str | None,
    build_digest: str | None,
) -> str:
    return (
        f"agent {version or 'version unavailable'} "
        f"(binary {_short_digest(binary_digest)}, build {_short_digest(build_digest)})"
    )


def _short_digest(value: str | None) -> str:
    if value is None:
        return "unavailable"
    prefix = "sha256:" if value.startswith("sha256:") else ""
    digest = value.removeprefix("sha256:")
    return f"{prefix}{digest[:12]}..."


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
