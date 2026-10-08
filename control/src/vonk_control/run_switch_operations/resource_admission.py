"""Resource admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    RunSwitchCode,
)
from vonk_forge_contracts import ModelDefinition

from ..models import (
    CatalogDocumentRevision,
)
from ..resource_planning import (
    resolve_effective_settings,
)
from ..run_switch_contract import (
    RunSwitchAssessment,
    RunSwitchPreviewRequest,
    RunSwitchReason,
    StopImpact,
)
from ..strict_json import (
    read_stored_model,
)
from .build_helpers import _refreshed_freshness
from .constants import (
    _MEMORY_CAPACITY_REFUSALS,
    _MEMORY_STOP_CONDITIONAL_REFUSALS,
    _active_recipe_revision,
)
from .errors import RunSwitchRequestInvalid
from .interfaces import _ResourceFits
from .planning_helpers import (
    _conditional_post_stop_memory_check,
    _now,
    _resource_reason,
    _settings_view,
)

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class ResourceAdmissionMixin:
    def recheck_resources_in_session(
        self,
        session: Session,
        request: RunSwitchPreviewRequest,
        reviewed: RunSwitchAssessment,
        *,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> RunSwitchAssessment:
        """Refresh SQL-owned resource facts without reopening artifact admission.

        The caller fences inventory/reservation writers and validates the exact
        reviewed stop set before this call. Cache, build and capability-provider
        work remains outside this transaction. Preparation evidence here stays
        the reviewed snapshot; this method makes no new availability claim.
        """
        service = typing_cast("RunSwitchOperationService", self)
        revision = _active_recipe_revision(session, request.recipe_revision_id)
        if revision is None:
            raise RunSwitchRequestInvalid(RunSwitchCode.RECIPE_UNRESOLVED)
        _, model_documents, blockers = service._resolve_documents(
            session,
            revision,
            request.model_content_sha256,
            requested_recipe_digest=revision.content_digest,
        )
        resolution = resolve_effective_settings(revision.document)
        blockers.extend(
            _resource_reason(
                reason,
                node_ids=tuple(node.node_id for node in request.spark_group.nodes),
            )
            for reason in resolution.reasons
        )
        resources = service._resource_fits(
            session,
            revision,
            request,
            now=_now(service._clock),
            stops=reviewed.stops,
            placement_blockers=blockers,
            effective_settings=resolution.settings,
            model_documents=model_documents,
            image_bytes=reviewed.preparation.runtime_image.image_bytes
            if reviewed.preparation is not None
            else None,
            artifact_bytes=reviewed.preparation.model.artifact_set_bytes
            if reviewed.preparation is not None
            else None,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )
        blockers.extend(resources.blockers)
        return read_stored_model(
            RunSwitchAssessment,
            {
                **{
                    name: getattr(reviewed, name)
                    for name in RunSwitchAssessment.model_fields
                },
                # The fit above read the Spark inventory as it is now, and its
                # memory evidence names that sample. Carry the same samples, not
                # the reviewed ones: an inventory refreshed since the review
                # (a parked admission is retried minutes later) would otherwise
                # fail the assessment's own sample binding on every retry.
                "freshness": _refreshed_freshness(
                    reviewed.freshness, resources.freshness
                ),
                "allowed": not blockers,
                "blockers": blockers,
                "fit_current": resources.current,
                "fit_after_stop": resources.after_stop,
                "post_stop_memory_check": resources.post_stop_memory_check,
                "effective_settings": _settings_view(resolution.settings)
                if resolution.settings is not None
                else None,
                "stop_before_prepare": resources.stop_before_prepare,
                "stop_before_transfer": resources.stop_before_transfer,
            },
        )

    def _resource_fits(
        self,
        session: Session,
        revision: CatalogDocumentRevision,
        request: RunSwitchPreviewRequest,
        *,
        now: datetime,
        stops: Sequence[StopImpact],
        placement_blockers: Sequence[RunSwitchReason],
        effective_settings: object | None,
        model_documents: Mapping[tuple[str, str, str], ModelDefinition],
        image_bytes: int | None = None,
        artifact_bytes: int | None = None,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> _ResourceFits:
        service = typing_cast("RunSwitchOperationService", self)
        (
            freshness,
            current,
            current_blockers,
            current_warnings,
            current_memory_shortfalls,
        ) = service._fit(
            session,
            revision,
            request.spark_group,
            now=now,
            excluded_run_ids=(),
            effective_settings=effective_settings,
            model_documents=model_documents,
            serving=request.action != "install",
            revision_digest=revision.content_digest,
            image_bytes=image_bytes,
            artifact_bytes=artifact_bytes,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )
        current = service._placement_fit(current, placement_blockers)
        after_stop = None
        blockers = current_blockers
        conditional = None
        if stops:
            (
                _,
                after_stop,
                after_stop_blockers,
                after_stop_warnings,
                after_stop_memory_shortfalls,
            ) = service._fit(
                session,
                revision,
                request.spark_group,
                now=now,
                excluded_run_ids=tuple(stop.run_id for stop in stops),
                effective_settings=effective_settings,
                model_documents=model_documents,
                serving=request.action != "install",
                revision_digest=revision.content_digest,
                image_bytes=image_bytes,
                artifact_bytes=artifact_bytes,
                excluded_profile_application_ids=excluded_profile_application_ids,
            )
            after_stop = service._placement_fit(after_stop, placement_blockers)
            warnings = list(current_warnings)
            for warning in after_stop_warnings:
                if warning not in warnings:
                    warnings.append(warning)

            current_memory_blockers = tuple(
                reason
                for reason in current_blockers
                if reason.code in _MEMORY_STOP_CONDITIONAL_REFUSALS
            )
            if current_memory_blockers:
                # A post-stop fit is still based on the pre-stop inventory.
                # When current memory fit depends on an exact stop, publish
                # only the stop-bound condition and let the existing fresh
                # post-stop inventory gate decide whether dispatch can start.
                post_stop_blockers_are_memory_only = all(
                    reason.code in _MEMORY_CAPACITY_REFUSALS
                    for reason in after_stop.blockers
                )
                memory_shortfalls = {
                    node_id: current_memory_shortfalls.get(node_id, frozenset())
                    | after_stop_memory_shortfalls.get(node_id, frozenset())
                    for node_id in set(current_memory_shortfalls)
                    | set(after_stop_memory_shortfalls)
                }
                if post_stop_blockers_are_memory_only:
                    conditional = _conditional_post_stop_memory_check(
                        session,
                        stops,
                        current,
                        (
                            *current_memory_blockers,
                            *(
                                reason
                                for reason in after_stop.blockers
                                if reason.code in _MEMORY_CAPACITY_REFUSALS
                            ),
                        ),
                        memory_shortfalls,
                    )
                blockers = list(current_memory_blockers)
                for reason in after_stop.blockers:
                    if reason not in blockers:
                        blockers.append(reason)
                after_stop = None
            else:
                conditional = _conditional_post_stop_memory_check(
                    session,
                    stops,
                    after_stop,
                    after_stop.blockers,
                    after_stop_memory_shortfalls,
                )
                blockers = after_stop_blockers
            if conditional is not None:
                after_stop = None
                blockers = []
        else:
            warnings = current_warnings
        return _ResourceFits(
            freshness,
            current,
            after_stop,
            current_blockers,
            blockers,
            warnings,
            conditional,
        )
