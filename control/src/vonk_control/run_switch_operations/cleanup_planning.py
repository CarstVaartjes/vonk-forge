"""Cleanup planning."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Literal,
)
from typing import cast as typing_cast

from sqlalchemy import select
from vonk_agent_protocol import (
    RunSwitchCode,
    SecurityRefusalError,
    UninstallPlanCode,
    UnknownOutcomeError,
    run_switch_code,
)

from ..categorized_errors import (
    MissingRecord,
)
from ..models import (
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
)
from ..recipe_operations import (
    RecipeOperationConflict,
)
from ..run_switch_contract import (
    InvocationMetadata,
    RunSwitchCleanupPreviewRequest,
    RunSwitchPlan,
    RunSwitchReason,
    RunSwitchReconciliationAuthority,
    SparkGroup,
    SparkGroupNode,
)
from ..strict_json import read_stored_model
from .constants import _active_recipe_revision
from .planning_helpers import _as_reason, _now

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class CleanupPlanningMixin:
    def _preview_cleanup_once(
        self,
        request: RunSwitchCleanupPreviewRequest | str,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        """Plan one scoped removal without consulting launch readiness.

        The installation's own uninstall assessment is the only authority for
        whether the removal may proceed.  Capacity, freshness, catalog and
        cache findings stay diagnostics, because being unable to start work must
        never prevent removing work.
        """
        service = typing_cast("RunSwitchOperationService", self)

        installation_id = (
            request if isinstance(request, str) else request.installation_id
        )
        cleanup_mode: Literal["uninstall", "reconcile"] = (
            "uninstall" if isinstance(request, str) else request.cleanup_mode
        )
        invocation = InvocationMetadata()
        now = _now(service._clock)
        with service._sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            if installation is None:
                raise MissingRecord(installation_id)
            revision = (
                session.get(CatalogDocumentRevision, installation.recipe_revision_id)
                if cleanup_mode == "reconcile"
                else _active_recipe_revision(session, installation.recipe_revision_id)
            )
            if revision is not None and revision.kind != "recipe":
                revision = None
            mapping = session.get(ClusterMapping, installation.mapping_id)
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == installation.mapping_id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            group = SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.endpoint_owner,
                    )
                    for node in mapping_nodes
                ]
            )
            model_digest = installation.model_content_sha256
            recipe_digest = revision.content_digest if revision is not None else None
            (
                _model_document,
                _model_documents,
                document_warnings,
            ) = service._resolve_documents(
                session,
                revision,
                model_digest,
                requested_recipe_digest=recipe_digest,
            )
            (
                freshness,
                fit_current,
                _fit_blockers,
                fit_warnings,
                _memory_shortfalls,
            ) = service._fit(
                session,
                revision,
                group,
                now=now,
                # The installation's own runs are being removed, so they are not
                # capacity this plan has to fit alongside.
                excluded_run_ids=tuple(
                    session.scalars(
                        select(RecipeRun.id).where(
                            RecipeRun.installation_id == installation_id
                        )
                    )
                ),
            )
            inspection = service._inspect_artifacts(
                session,
                model_digest,
                revision.id if revision is not None else None,
                group,
                retention="retain-cached",
                now=now,
            )
            build = (
                session.get(RecipeBuild, installation.recipe_build_id)
                if installation.recipe_build_id is not None
                else None
            )
            build_candidate = build or (
                service._latest_build(session, revision.id)
                if revision is not None
                else None
            )
            build_evidence, runtime_storage, _build_blockers, _build_warnings = (
                service._build_evidence(
                    session,
                    revision,
                    build,
                    build_candidate,
                    group,
                    require_available=False,
                )
            )
            node_ids = [node.node_id for node in group.nodes]
            blockers: list[RunSwitchReason] = []
            warnings = [*document_warnings, *fit_warnings, *inspection.warnings]
            cleanup_disposition: Literal["uninstall", "abandon"] = "uninstall"
            reconciliation_authority: RunSwitchReconciliationAuthority | None = None
            if service._lifecycle is None:
                blockers.append(
                    _as_reason(
                        (
                            RunSwitchCode.RECONCILIATION_ASSESSMENT_UNAVAILABLE
                            if cleanup_mode == "reconcile"
                            else RunSwitchCode.UNINSTALL_ASSESSMENT_UNAVAILABLE
                        ),
                        "Cleanup cannot be assessed without the lifecycle service.",
                        scope="operation",
                        node_ids=node_ids,
                    )
                )
            elif cleanup_mode == "reconcile" and service._never_installed(
                installation_id
            ):
                # Reconciling a plan that never reached a node discards the
                # record: there is no effect on a Spark to reconcile.
                cleanup_disposition = "abandon"
            elif cleanup_mode == "reconcile":
                try:
                    authority = service._lifecycle.preview_reconciliation_authority(
                        installation_id,
                        session=session,
                        allow_active_reconciliation=True,
                    )
                    reconciliation_authority = authority
                except (UnknownOutcomeError, SecurityRefusalError):
                    raise
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.RECONCILIATION_ASSESSMENT_UNAVAILABLE,
                            f"The installation cannot be represented by an exact reconciliation authority: {error}",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
                else:
                    if (
                        service._lifecycle.assess_superseded_unissued(
                            "recipe.reconcile", installation_id
                        )
                        or service._lifecycle.assess_superseded_issued(
                            "recipe.reconcile", installation_id
                        )
                        is not None
                    ):
                        warnings.append(
                            _as_reason(
                                RunSwitchCode.RECONCILIATION_PREREQUISITE,
                                "The prior exact reconciliation attempt will be retired or observed before new cleanup is queued.",
                                scope="operation",
                                node_ids=node_ids,
                                severity="warning",
                            )
                        )
                    completed_nodes = [
                        target.node_id
                        for target in reconciliation_authority.targets
                        if target.state == "reconciled"
                    ]
                    if completed_nodes:
                        warnings.append(
                            _as_reason(
                                RunSwitchCode.RECONCILIATION_RECEIPTS_RETAINED,
                                "Previously verified node cleanup receipts will be reused.",
                                scope="node",
                                node_ids=completed_nodes,
                                severity="warning",
                            )
                        )
            else:
                try:
                    assessment = service._lifecycle.preview_uninstall(installation_id)
                except (UnknownOutcomeError, SecurityRefusalError):
                    raise
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    assessment = None
                if assessment is None:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.UNINSTALL_ASSESSMENT_UNAVAILABLE,
                            "The installation cannot be represented by a safe uninstall plan.",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
                elif not assessment.allowed:
                    issued = service._lifecycle.assess_superseded_issued(
                        "recipe.uninstall", installation_id
                    )
                    for blocker in assessment.blockers:
                        reason = _as_reason(
                            RunSwitchCode.UNINSTALL_ISSUED_PREREQUISITE
                            if issued is not None
                            and blocker.code == UninstallPlanCode.OPERATION_ACTIVE
                            else RunSwitchCode.UNINSTALL_BLOCKED,
                            (
                                "The prior issued uninstall will be cancelled and "
                                "observed before this cleanup starts."
                                if issued is not None
                                and blocker.code == UninstallPlanCode.OPERATION_ACTIVE
                                else f"{blocker.code}: {blocker.detail}"
                            ),
                            scope="operation",
                            node_ids=node_ids,
                            severity=(
                                "warning"
                                if issued is not None
                                and blocker.code == UninstallPlanCode.OPERATION_ACTIVE
                                else "blocker"
                            ),
                        )
                        if reason.severity == "warning":
                            warnings.append(reason)
                        else:
                            blockers.append(reason)
                else:
                    # The installation's own assessment decides whether this is
                    # a removal or an abandonment; the phase executor reads the
                    # same disposition rather than re-deriving it.
                    cleanup_disposition = assessment.disposition
                    for warning in assessment.warnings:
                        warnings.append(
                            _as_reason(
                                run_switch_code(warning.code),
                                warning.detail,
                                scope="operation",
                                node_ids=node_ids,
                                severity="warning",
                            )
                        )
            phases = service._phases(
                action="cleanup",
                group=group,
                installation_id=installation.id,
                installation_state=installation.state,
                stops=[],
                inspection=inspection,
                runtime_storage=runtime_storage,
                retention="retain-cached",
                blockers=blockers,
                stop_before_transfer=False,
                stop_before_prepare=False,
                cleanup_disposition=cleanup_disposition,
                cleanup_mode=cleanup_mode,
            )
            storage = service._storage(inspection, retention="retain-cached")
            preparation = service._preparation(
                revision=revision,
                group=group,
                inspection=inspection,
                build=build,
                build_candidate=build_candidate,
                runtime_storage=runtime_storage,
                now=now,
                reasons=[*blockers, *warnings],
            )
            plan_data = read_stored_model(
                RunSwitchPlan,
                {
                    "schema_version": 2,
                    "generated_at": now,
                    "action": "cleanup",
                    "model_content_sha256": model_digest,
                    "recipe_revision_id": revision.id if revision is not None else None,
                    "recipe_content_sha256": recipe_digest,
                    "alias": None,
                    "run_id": None,
                    "spark_group": group,
                    "mapping": service._mapping_selection(mapping, mapping_nodes),
                    "installation_id": installation.id,
                    "installation_state": installation.state,
                    "cleanup_disposition": cleanup_disposition,
                    "cleanup_mode": cleanup_mode,
                    "reconciliation_authority": reconciliation_authority,
                    "recipe_build_id": installation.recipe_build_id,
                    "image_digest": installation.image_digest,
                    "start_plan_digest": None,
                    "freshness": freshness,
                    "fit_current": fit_current,
                    "fit_after_stop": None,
                    "fit": fit_current,
                    "storage": storage,
                    "runtime_storage": runtime_storage,
                    "build": build_evidence,
                    "preparation": preparation,
                    "conflicts": [],
                    "stops": [],
                    "reclaimed_bytes": 0,
                    "phases": phases,
                    "allowed": not blockers,
                    "blockers": blockers,
                    "warnings": warnings,
                    "invocation": invocation,
                    "plan_digest": "0" * 64,
                    "stop_before_prepare": False,
                },
            )
            return service._finalize_plan(plan_data)
