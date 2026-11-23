from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.agent_words import Disposition
from vonk_agent_protocol.build_import import RecipeBuildEvidence
from vonk_agent_protocol.contracts import AgentFailureResult, canonical_message
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_agent_protocol.reason_codes.recipes import RecipeImageCode
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_forge_contracts import RecipeDefinition

from ..failure_classification import disposition, is_security_failure
from ..failure_evidence import safe_text
from ..jobs import states as job_states
from ..models.cache import RecipeBuild
from ..models.operations import AgentOperation, AgentOperationAttempt
from ..operations.blockers import OperationBlocker, make_blocker
from ..prebuilt_images import PrebuiltDecision
from ..recipe_builds.common import _valid_succeeded_receipt
from ..recipe_image_availability.contracts import (
    BuildUnsettled,
    RecipeImageAvailabilityError,
)
from ..recipe_operations.execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_policy,
)
from ..recipe_operations.runtime_specs import (
    ResolvedRecipe,
    compile_runtime_spec,
)
from ..recovery_policy import kind_for_failure_fields
from ..runtime.spec_contract import SpecRuntime
from ..runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from ..strict_json import read_stored_model


# Controller prebuilt pulls hold no Spark or builder slot while waiting.
class AvailabilityUnsettled(UnknownOutcomeError, RecipeImageAvailabilityError):
    """Unknown build evidence re-observed by the bounded request owner."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        reason: WaitReason,
        **fields: Any,
    ) -> None:
        RecipeImageAvailabilityError.__init__(self, code, detail, **fields)
        self.typed_reason = reason


def _prebuilt_blocker(decision: PrebuiltDecision) -> OperationBlocker:
    """The named reason a plan did or did not use the catalog's prebuilt image."""

    return make_blocker(
        decision.code,
        decision.detail,
        severity="info" if decision.used else "warning",
    )


def _capacity_wait(
    skipped: Mapping[str, tuple[str, str]],
    candidate_ids: tuple[str, ...],
    prebuilt_unused: PrebuiltDecision | None = None,
) -> BuildUnsettled:
    """A wait for a builder that names every Spark passed over and why.

    When the catalog's prebuilt image could not be used, that comes first:
    it is why a Spark build was needed at all.
    """

    if not candidate_ids:
        blockers: list[OperationBlocker] = [
            make_blocker(
                RecipeImageCode.NO_BUILDER,
                "No active linux-arm64 Spark is enrolled to build this image.",
                severity="error",
            )
        ]
    else:
        blockers = [
            make_blocker(code, detail, node_ids=[node_id])
            for node_id, (code, detail) in sorted(skipped.items())
        ] or [
            make_blocker(
                RecipeImageCode.BUILD_CAPACITY_WAIT,
                "No Spark could be selected to build this image yet.",
            )
        ]
    detail = "no Spark can build this image right now: " + "; ".join(
        f"{item.node_ids[0][:12] if item.node_ids else 'fleet'}: {item.code}"
        for item in blockers[:6]
    )
    if prebuilt_unused is not None:
        blockers.insert(0, _prebuilt_blocker(prebuilt_unused))
        detail = f"prebuilt image not used ({prebuilt_unused}); {detail}"
    return BuildUnsettled(
        RecipeImageCode.BUILD_CAPACITY_WAIT,
        detail[:512],
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
        retryable=True,
        recovery_actions=("resume", "retry"),
        blockers=tuple(blockers),
    )


def _recorded_decision(policy: object) -> PrebuiltDecision | None:
    try:
        stored = parse_stored_build_policy(policy).prebuilt_decision
    except RecipeExecutionContractError:
        return None
    return None if stored is None else PrebuiltDecision(stored.code, stored.detail)


def _prebuilt_choice(
    session: Session, build_id: str
) -> tuple[PrebuiltDecision | None, str | None]:
    """The prebuilt decision a build was planned with, and its image if used.

    Builds planned before decisions were recorded have none: they show the
    wait without one rather than a guess.
    """

    build = session.get(RecipeBuild, build_id)
    if build is None:
        return None, None
    try:
        policy = parse_stored_build_policy(build.policy_report)
    except RecipeExecutionContractError:
        return None, None
    return _recorded_decision(build.policy_report), policy.prebuilt_image


#: Podman closes a failed build's stderr by echoing the step it ended at,
#: ``building at STEP "<step>": <cause>``; the agent reports the step as its
#: own ``build_step`` property. A RUN step's echo ends ``": while running
#: runtime:``, which is all that is left of it once an older agent's tail clipped
#: the front of a long echo.
_PODMAN_STEP_ECHO = 'building at STEP "'


_PODMAN_RUN_CLOSE = '": while running runtime:'


def _without_step_echo(text: str, *, clipped: bool) -> str:
    """``text`` with Podman's echo of the failed step reduced to its cause.

    A RUN step ends at its ``": while running runtime:`` close, any other at
    the first ``": `` after the marker (the agent's rule), and the cause after
    it is kept: a COPY names its missing source there. A tail
    whose front was clipped may hold only the end of a long or heredoc echo;
    everything up to its RUN close is the echo.
    """
    marker = text.rfind(_PODMAN_STEP_ECHO)
    if marker >= 0:
        start = text.rfind("\n", 0, marker) + 1
        # A RUN echo ends at its close (a heredoc may hold ``": `` itself);
        # a step without one (COPY, ADD) at the first ``": ``.
        command = marker + len(_PODMAN_STEP_ECHO)
        close = text.find(_PODMAN_RUN_CLOSE, command)
        if close < 0:
            close = text.find('": ', command)
        return text[:start] + (text[close + 3 :] if close >= 0 else "")
    close = text.find(_PODMAN_RUN_CLOSE)
    if clipped and close >= 0:
        return text[close + 3 :]
    return text


def _keep_end(text: str, limit: int) -> str:
    """The end of ``text`` within ``limit`` characters, from a line start."""
    if len(text) <= limit:
        return text
    end = text[-limit:]
    newline = end.find("\n")
    return end[newline + 1 :] if 0 <= newline < len(end) - 1 else end


def _step_output_tail(diagnostics: FailureDiagnostics, limit: int) -> str:
    """The failed step's own last output, each stream with its own share.

    A step prints to stdout and stderr, and Podman then echoes the whole step on
    stderr. Choosing stderr because it was present kept that echo and none of
    the lines the step printed; one joined tail let either stream's noise crowd
    out the other's cause (pip's error is on stdout, its noise on stderr).
    """
    streams = [
        "\n".join(line for line in text.splitlines() if line.strip())
        for text in (
            safe_text(diagnostics.stdout.text),
            _without_step_echo(
                safe_text(diagnostics.stderr.text),
                clipped=diagnostics.stderr.truncated,
            ),
        )
    ]
    streams = [text for text in streams if text]
    if len(streams) < 2:
        return _keep_end(streams[0], limit) if streams else ""
    stdout, stderr = streams
    # Each stream gets half; one shorter than its half leaves the rest to the other.
    stdout_share = max((limit - 1) // 2, limit - 1 - len(stderr))
    stderr_share = limit - 1 - min(len(stdout), stdout_share)
    return f"{_keep_end(stdout, stdout_share)}\n{_keep_end(stderr, stderr_share)}"


def _observe_build(
    sessions: sessionmaker[Session],
    operation: Any,
    *,
    builder_node_id: str,
    build_input_sha256: str,
    progress: Callable[[Mapping[str, object]], None],
    storage: FilesystemRuntimeImageStorage,
) -> Mapping[str, object] | BuildUnsettled:
    if operation.state == "cancelled":
        return BuildUnsettled(
            RecipeImageCode.BUILD_CANCELLED,
            "accepted build was cancelled",
            settled_build_operation_id=operation.id,
            retryable=True,
            reason=WaitReason.SCOPE_CHANGED,
        )
    if operation.state in job_states.LIVE:
        with sessions() as session:
            current_progress = _build_progress(session, operation.id, builder_node_id)
            decision, prebuilt_image = _prebuilt_choice(session, operation.owner_id)
        if current_progress is not None:
            progress(current_progress.model_dump(mode="json", exclude_none=True))
        if prebuilt_image is not None:
            # The Controller pulls the image; no Spark builds anything, even
            # though the build job names a Spark as its nominal builder.
            wait = (
                f"waiting for the Controller to pull prebuilt image {prebuilt_image} "
                f"(build {operation.id}): {operation.state}"
            )
        else:
            wait = (
                f"waiting for build {operation.id} on {builder_node_id}: "
                f"{operation.state}"
            )
        blockers = [make_blocker(RecipeImageCode.BUILD_WAIT, wait)]
        if decision is not None:
            blockers.insert(0, _prebuilt_blocker(decision))
        return BuildUnsettled(
            RecipeImageCode.BUILD_WAIT,
            wait,
            retryable=True,
            retry_after_seconds=5,
            blockers=tuple(blockers),
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            dependency_active=True,
        )
    if operation.state in job_states.words(LifecycleState.FAILED):
        aggregate = operation.result
        node_evidence = (
            aggregate.get("node_evidence") if isinstance(aggregate, Mapping) else None
        )
        raw_failure = (
            node_evidence.get(builder_node_id)
            if isinstance(node_evidence, Mapping)
            else None
        )
        if not isinstance(raw_failure, Mapping):
            return BuildUnsettled(
                RecipeImageCode.BUILD_INVALID,
                "canonical Recipe build failure evidence is missing or invalid",
                step="build",
                reason=WaitReason.RECEIPT_MISSING,
            )
        try:
            failure = read_stored_model(
                AgentFailureResult, canonical_message(raw_failure), from_json=True
            )
        except (TypeError, ValueError):
            return BuildUnsettled(
                RecipeImageCode.BUILD_INVALID,
                "canonical Recipe build failure evidence is missing or invalid",
                step="build",
                reason=WaitReason.RECEIPT_MISSING,
            )
        # A returned deterministic failure must end this preparation too.
        # Reissuing fresh children here bypassed the agent queue's fail-fast
        # decision and hammered the same broken build indefinitely.
        retryable = disposition(
            kind_for_failure_fields(failure.failure_kind, failure.error_code)
        ) in {Disposition.WAIT, Disposition.OBSERVE} and not is_security_failure(
            failure.error_code or ""
        )
        summary = failure.summary or failure.reason or "canonical Recipe build failed"
        category = (
            failure.diagnostics.category if failure.diagnostics is not None else None
        )
        detail = f"{category}: {summary}" if category is not None else summary
        diagnostic = failure.diagnostic
        if failure.diagnostics is not None:
            diagnostics = failure.diagnostics
            # The preparation detail is what acceptance and blockers surface.
            # Reserve room within its 512 characters for the exit and final
            # output, including the stage prefix added by failure projection.
            parts: list[str] = []
            if diagnostics.process_exit_code is not None:
                parts.append(f"exit code {diagnostics.process_exit_code}")
            if tail := _step_output_tail(diagnostics, 256):
                parts.append(tail)
            if parts:
                detail = "; ".join([detail[:128], *parts])
            diagnostic = _step_output_tail(diagnostics, 1024) or diagnostic
        return BuildUnsettled(
            failure.error_code or RecipeImageCode.BUILD_FAILED,
            detail,
            retryable=retryable,
            retry_after_seconds=(failure.retry_after_seconds if retryable else None),
            log_excerpt=diagnostic or None,
            step=failure.stage or "build",
            settled_build_operation_id=operation.id,
            reason=WaitReason.REPORT_UNCERTAIN,
        )
    if operation.state != "succeeded" or not isinstance(operation.result, Mapping):
        return BuildUnsettled(
            RecipeImageCode.BUILD_INVALID,
            "canonical Recipe build outcome is invalid",
            reason=WaitReason.REPORT_UNCERTAIN,
        )
    aggregate = operation.result
    successful_nodes = aggregate.get("successful_nodes")
    node_evidence = aggregate.get("node_evidence")
    raw_evidence = (
        node_evidence.get(builder_node_id)
        if isinstance(node_evidence, Mapping)
        else None
    )
    if (
        not isinstance(successful_nodes, list)
        or builder_node_id not in successful_nodes
        or not isinstance(raw_evidence, Mapping)
    ):
        return BuildUnsettled(
            RecipeImageCode.BUILD_INVALID,
            "canonical Recipe build evidence is incomplete",
            reason=WaitReason.RECEIPT_MISSING,
        )
    try:
        evidence = read_stored_model(
            RecipeBuildEvidence,
            json.dumps(dict(raw_evidence)),
            from_json=True,
        )
    except (TypeError, ValueError):
        return BuildUnsettled(
            RecipeImageCode.BUILD_INVALID,
            "canonical Recipe build evidence is invalid",
            reason=WaitReason.REPORT_UNCERTAIN,
        )
    # The Spark uploads a Docker archive; the Controller then stores it as the
    # runtime image and rewrites the build row to that image. The row, not the
    # agent's upload report, names the image this preparation adopts, and an
    # upload not stored yet is a wait: treating it as cache loss rebuilt it.
    with sessions() as session:
        build = session.get(RecipeBuild, operation.owner_id)
        if (
            build is not None
            and build.build_input_sha256 == build_input_sha256
            and _valid_succeeded_receipt(build)
        ):
            assert build.oci_layout_sha256 is not None
            if (storage.root / build.oci_layout_sha256).is_file():
                wait = f"waiting for the Controller to store build {operation.owner_id}"
                return BuildUnsettled(
                    RecipeImageCode.BUILD_WAIT,
                    wait,
                    retryable=True,
                    retry_after_seconds=5,
                    blockers=(make_blocker(RecipeImageCode.BUILD_WAIT, wait),),
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            evidence = evidence.model_copy(
                update={
                    "image_digest": build.image_digest,
                    "oci_layout_sha256": build.oci_layout_sha256,
                    "image_bytes": build.image_bytes,
                }
            )
    return evidence.model_dump(mode="json") | {
        "state": operation.state,
        "build_id": operation.owner_id,
        "build_input_sha256": build_input_sha256,
        "builder_node_id": builder_node_id,
    }


def _cached_build_receipt(resolution: Any) -> Mapping[str, object] | None:
    """Shape one prepared-build resolution into the canonical build receipt.

    This is the only source-build reuse test. The resolution proves the exact
    archive is present on disk; when its verification receipt also exists the
    values come from that receipt, and when the receipt is missing or
    incomplete the values come from the SQL build candidate so preparation
    re-verifies the bytes and republishes the receipt instead of building
    again. Queue-time and dispatch-time callers share this one answer.
    """

    if getattr(resolution, "cached", False) is not True:
        return None
    identity = (
        resolution.build_id,
        resolution.builder_node_id,
        resolution.build_input_sha256,
        resolution.image_digest,
        resolution.oci_layout_sha256,
    )
    if (
        not all(isinstance(value, str) for value in identity)
        or not isinstance(resolution.image_bytes, int)
        or isinstance(resolution.image_bytes, bool)
        or resolution.image_bytes < 1
    ):
        # Incomplete cache bookkeeping authorizes no reuse. The request enters
        # normal preparation, which verifies or rebuilds the exact artifact.
        return None
    return {
        "state": "succeeded",
        "build_id": resolution.build_id,
        "builder_node_id": resolution.builder_node_id,
        "build_input_sha256": resolution.build_input_sha256,
        "image_digest": resolution.image_digest,
        "oci_layout_sha256": resolution.oci_layout_sha256,
        "image_bytes": resolution.image_bytes,
    }


def _compile_consistent_runtime(
    recipe: RecipeDefinition,
    *,
    resolved: ResolvedRecipe,
    package_handle: object,
) -> dict[str, object]:
    """Compile every canonical role/rank and require one image identity."""

    roles = tuple(recipe.topology.roles)
    if not roles:
        raise AvailabilityUnsettled(
            RecipeImageCode.RUNTIME_INVALID,
            "canonical recipe has no topology roles",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    compiled: list[SpecRuntime] = []
    first_rank = 0
    models = resolved.models
    for role in roles:
        role_count = int(role.count)
        for rank in range(first_rank, first_rank + role_count):
            projection = compile_runtime_spec(
                recipe,
                recipe_digest=resolved.recipe_digest,
                models=models,
                package_handle=package_handle,
                role=role.name,
                rank=rank,
            )
            compiled.append(projection.runtime)
        first_rank += role_count
    first = compiled[0]
    identity = (first.image, first.architecture)
    if any(
        (runtime.image, runtime.architecture) != identity for runtime in compiled[1:]
    ):
        raise AvailabilityUnsettled(
            RecipeImageCode.RUNTIME_INVALID,
            "canonical recipe roles do not share one runtime image identity",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return first.document()


def _build_progress(
    session: Session, job_id: str, node_id: str
) -> OperationProgress | None:
    """Read the current attempt's typed heartbeat, including image uploads."""
    document = session.scalar(
        select(AgentOperationAttempt.progress)
        .join(
            AgentOperation,
            AgentOperation.id == AgentOperationAttempt.operation_id,
        )
        .where(
            AgentOperation.parent_job_id == job_id,
            AgentOperation.node_id == node_id,
            AgentOperationAttempt.attempt == AgentOperation.current_attempt,
        )
    )
    if document is None:
        return None
    return read_stored_model(OperationProgress, json.dumps(document), from_json=True)
