"""Secret-free operator diagnostics for controller-managed agent upgrades."""

from __future__ import annotations

from collections.abc import Mapping

LEGACY_GENERIC_AGENT_UPGRADE_REASONS = frozenset(
    {
        "agent upgrade request is invalid",
        "agent upgrade helper rejected the request",
        "agent upgrade helper rejected the request: operation_failed",
    }
)

RECOVERABLE_AGENT_UPGRADE_REASONS = frozenset(
    {
        *LEGACY_GENERIC_AGENT_UPGRADE_REASONS,
        "agent upgrade helper is unavailable",
        "agent upgrade did not restart the service",
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
    """Explain an ambiguous legacy failure without rewriting its raw evidence."""

    if raw_reason not in LEGACY_GENERIC_AGENT_UPGRADE_REASONS:
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
        f"{attempt_label}; target {expected} was not proven. The legacy helper "
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
