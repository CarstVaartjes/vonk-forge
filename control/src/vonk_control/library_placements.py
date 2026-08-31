"""One-shot Library placement façade over the durable Fleet-profile coordinator."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vonk_agent_protocol import canonical_message

from .fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileNode,
    FleetProfilePreview,
)
from .fleet_profiles import FleetProfileConflict
from .library_placement_contract import (
    LibraryPlacementApplication,
    LibraryPlacementApplyRequest,
    LibraryPlacementLocations,
    LibraryPlacementNode,
    LibraryPlacementPreview,
    LibraryPlacementPreviewRequest,
    LibraryPlacementReason,
    LibraryPlacementStep,
)


class LibraryPlacementConflict(RuntimeError):
    """A selected Spark group is invalid, blocked, or stale."""


@dataclass(frozen=True, slots=True)
class _PreparedPlacement:
    preview: LibraryPlacementPreview
    assignment: FleetProfileAssignmentInput | None
    profile_id: str | None
    profile_plan_digest: str | None


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


class LibraryPlacementService:
    """Translate pointer or keyboard placement into the same durable workflow."""

    def __init__(self, projection: Any, profiles: Any) -> None:
        self._projection = projection
        self._profiles = profiles

    def preview(
        self, value: LibraryPlacementPreviewRequest, *, actor: str
    ) -> LibraryPlacementPreview:
        return self._prepare(value, actor=actor).preview

    def apply(
        self, value: LibraryPlacementApplyRequest, *, actor: str
    ) -> LibraryPlacementApplication:
        try:
            replay = self._profiles.replay_internal_placement(value.request_key)
        except FleetProfileConflict as error:
            raise LibraryPlacementConflict(str(error)) from error
        if replay is not None:
            raw = replay.progress.get("library_placement")
            if not isinstance(raw, Mapping) or not self._matches_request(raw, value):
                raise LibraryPlacementConflict(
                    "direct placement request key was reused for another plan"
                )
            return self._application(replay, raw)
        prepared = self._prepare(
            LibraryPlacementPreviewRequest.model_validate(
                value.model_dump(exclude={"plan_digest", "request_key"}, mode="python")
            ),
            actor=actor,
        )
        if prepared.preview.plan_digest != value.plan_digest:
            raise LibraryPlacementConflict("Library placement preview is stale")
        if not prepared.preview.allowed:
            raise LibraryPlacementConflict("Library placement preview is blocked")
        if (
            prepared.assignment is None
            or prepared.profile_id is None
            or prepared.profile_plan_digest is None
        ):
            raise LibraryPlacementConflict("Library placement authority is unavailable")
        metadata = {
            "recipe_id": value.recipe_id,
            "recipe_revision_id": prepared.preview.recipe_revision_id,
            "selected_node_ids": list(value.node_ids),
            "desired_state": value.desired_state,
            "alias": value.alias,
            "profile_plan_digest": prepared.profile_plan_digest,
            "installation_ids": prepared.preview.locations.installation_ids,
            "run_ids": prepared.preview.locations.run_ids,
            "plan_digest": value.plan_digest,
        }
        try:
            result = self._profiles.apply_internal_placement(
                prepared.profile_id,
                profile_plan_digest=prepared.profile_plan_digest,
                placement_plan_digest=value.plan_digest,
                request_key=value.request_key,
                actor=actor,
                metadata=metadata,
            )
        except FleetProfileConflict as error:
            raise LibraryPlacementConflict(str(error)) from error
        return self._application(result, metadata)

    def application(self, application_id: str) -> LibraryPlacementApplication:
        result = self._profiles.application(application_id)
        raw = result.progress.get("library_placement")
        if not isinstance(raw, Mapping):
            raise KeyError(application_id)
        metadata = dict(raw)
        return self._application(result, metadata)

    def _prepare(
        self, value: LibraryPlacementPreviewRequest, *, actor: str
    ) -> _PreparedPlacement:
        try:
            detail = self._projection.detail(value.recipe_id)
        except KeyError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise LibraryPlacementConflict(
                "Library placement authority is unavailable"
            ) from error
        revision = detail.selected_revision
        topology = detail.topology
        blockers: list[LibraryPlacementReason] = []
        warnings: list[LibraryPlacementReason] = []
        selected_ids = list(value.node_ids)
        if revision is None or revision.lifecycle != "resolved":
            blockers.append(
                self._reason(
                    "placement.recipe_not_operable",
                    "The selected recipe does not have a resolved immutable revision.",
                    "error",
                    selected_ids,
                )
            )
        if topology is None:
            blockers.append(
                self._reason(
                    "placement.topology_unavailable",
                    "The selected recipe topology is unavailable.",
                    "error",
                    selected_ids,
                )
            )
        elif topology.node_count != len(selected_ids):
            blockers.append(
                self._reason(
                    "placement.spark_count_mismatch",
                    f"This recipe requires exactly {topology.node_count} Sparks.",
                    "error",
                    selected_ids,
                )
            )

        groups = [
            group
            for placement in detail.placement
            for group in (*placement.recommendations, *placement.rejected_groups)
        ]
        group = next(
            (item for item in groups if sorted(item.node_ids) == selected_ids), None
        )
        if (
            group is None
            and topology is not None
            and topology.node_count == len(selected_ids)
        ):
            blockers.append(
                self._reason(
                    "placement.group_evidence_unavailable",
                    "The bounded placement authority did not produce exact evidence for this Spark group.",
                    "error",
                    selected_ids,
                )
            )
        if group is not None:
            for reason in group.reasons:
                converted = self._reason(
                    reason.code, reason.detail, reason.severity, selected_ids
                )
                if reason.severity == "error":
                    blockers.append(converted)
                elif reason.severity == "warning":
                    warnings.append(converted)
            if not group.eligible and not blockers:
                blockers.append(
                    self._reason(
                        "placement.group_ineligible",
                        "The selected Spark group is not compatible with this recipe.",
                        "error",
                        selected_ids,
                    )
                )

        selected_nodes = self._selected_nodes(group)
        locations = self._locations(group)
        assignment: FleetProfileAssignmentInput | None = None
        profile_id: str | None = None
        profile_preview: FleetProfilePreview | None = None
        steps: list[LibraryPlacementStep] = []
        if (
            revision is not None
            and topology is not None
            and group is not None
            and group.eligible
            and not blockers
        ):
            assignment = FleetProfileAssignmentInput(
                recipe_revision_id=revision.id,
                topology_name=topology.name,
                desired_state=value.desired_state,
                alias=value.alias,
                nodes=[
                    FleetProfileNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.endpoint_owner,
                    )
                    for node in group.nodes
                ],
            )
            semantic_identity = {
                "schema_version": 1,
                "recipe_revision_id": revision.id,
                "topology_name": topology.name,
                "desired_state": value.desired_state,
                "alias": value.alias,
                "nodes": [node.model_dump(mode="json") for node in assignment.nodes],
            }
            profile_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vonk-forge:library-placement:{_digest(semantic_identity)}",
                )
            )
            try:
                self._profiles.ensure_internal_placement(
                    profile_id, assignment, actor=actor
                )
                profile_preview = self._profiles.preview(profile_id)
            except FleetProfileConflict as error:
                blockers.append(
                    self._reason(
                        "placement.workflow_blocked",
                        str(error)[:512],
                        "error",
                        selected_ids,
                    )
                )
            if profile_preview is not None:
                for reason in profile_preview.reasons:
                    converted = self._reason(
                        reason.code, reason.detail, reason.severity, selected_ids
                    )
                    if reason.severity == "error":
                        blockers.append(converted)
                    elif reason.severity == "warning":
                        warnings.append(converted)
                steps = [
                    LibraryPlacementStep(
                        index=step.index,
                        kind=step.kind,
                        label=step.label,
                        node_ids=list(step.node_ids),
                    )
                    for step in profile_preview.steps
                    if step.kind != "uninstall"
                ]
                if not steps:
                    steps = [
                        LibraryPlacementStep(
                            index=0,
                            kind="keep",
                            label="Placement is already in the requested state",
                            node_ids=selected_ids,
                        )
                    ]

        revision_id = (
            revision.id
            if revision is not None
            else "00000000-0000-4000-8000-000000000000"
        )
        topology_name = topology.name if topology is not None else "unavailable"
        profile_plan_digest = (
            profile_preview.plan_digest if profile_preview is not None else None
        )
        identity = {
            "schema_version": 1,
            "recipe_id": value.recipe_id,
            "recipe_revision_id": revision_id,
            "topology_name": topology_name,
            "desired_state": value.desired_state,
            "alias": value.alias,
            "selected_node_ids": selected_ids,
            "selected_nodes": [item.model_dump(mode="json") for item in selected_nodes],
            "steps": [item.model_dump(mode="json") for item in steps],
            "blockers": [item.model_dump(mode="json") for item in blockers],
            "warnings": [item.model_dump(mode="json") for item in warnings],
            "locations": locations.model_dump(mode="json"),
            "profile_plan_digest": profile_plan_digest,
        }
        preview = LibraryPlacementPreview(
            generated_at=detail.generated_at,
            recipe_id=value.recipe_id,
            recipe_revision_id=revision_id,
            recipe_title=detail.recipe.title,
            topology_name=topology_name,
            desired_state=value.desired_state,
            alias=value.alias,
            invocation=value.invocation,
            selected_node_ids=selected_ids,
            selected_nodes=selected_nodes,
            allowed=not blockers
            and profile_preview is not None
            and profile_preview.allowed,
            steps=steps,
            blockers=blockers,
            warnings=warnings,
            locations=locations,
            plan_digest=_digest(identity),
        )
        return _PreparedPlacement(
            preview=preview,
            assignment=assignment,
            profile_id=profile_id,
            profile_plan_digest=profile_plan_digest,
        )

    def _application(
        self, result: Any, metadata: Mapping[str, object]
    ) -> LibraryPlacementApplication:
        recipe_id = str(metadata["recipe_id"])
        node_ids = [str(item) for item in metadata["selected_node_ids"]]
        locations = self._current_locations(recipe_id, node_ids, metadata)
        progress = dict(result.progress)
        progress.pop("library_placement", None)
        return LibraryPlacementApplication(
            id=result.id,
            state=result.state,
            recipe_id=recipe_id,
            recipe_revision_id=str(metadata["recipe_revision_id"]),
            selected_node_ids=node_ids,
            desired_state=str(metadata["desired_state"]),
            alias=metadata.get("alias"),
            plan_digest=str(metadata["plan_digest"]),
            current_step=result.current_step,
            total_steps=result.total_steps,
            current_operation_id=result.current_operation_id,
            status_reason=result.status_reason,
            progress=progress,
            locations=locations,
            created_at=result.created_at,
            updated_at=result.updated_at,
        )

    def _current_locations(
        self,
        recipe_id: str,
        node_ids: list[str],
        metadata: Mapping[str, object],
    ) -> LibraryPlacementLocations:
        try:
            detail = self._projection.detail(recipe_id)
            groups = [
                group
                for placement in detail.placement
                for group in (*placement.recommendations, *placement.rejected_groups)
            ]
            group = next(
                (item for item in groups if sorted(item.node_ids) == sorted(node_ids)),
                None,
            )
            if group is not None:
                return self._locations(group)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            pass
        installation_ids = metadata.get("installation_ids", [])
        run_ids = metadata.get("run_ids", [])
        return LibraryPlacementLocations(
            installation_ids=list(installation_ids)
            if isinstance(installation_ids, list)
            else [],
            run_ids=list(run_ids) if isinstance(run_ids, list) else [],
            installed=bool(installation_ids),
            running=bool(run_ids),
        )

    @staticmethod
    def _matches_request(
        metadata: Mapping[str, object], value: LibraryPlacementApplyRequest
    ) -> bool:
        return (
            metadata.get("recipe_id") == value.recipe_id
            and metadata.get("selected_node_ids") == list(value.node_ids)
            and metadata.get("desired_state") == value.desired_state
            and metadata.get("alias") == value.alias
            and metadata.get("plan_digest") == value.plan_digest
        )

    @staticmethod
    def _selected_nodes(group: Any | None) -> list[LibraryPlacementNode]:
        if group is None:
            return []
        return [
            LibraryPlacementNode(
                node_id=node.node_id,
                rank=node.rank,
                role=node.role,
                endpoint_owner=node.endpoint_owner,
                disk_free_bytes=node.disk_free_bytes,
                disk_required_bytes=node.disk_required_bytes,
                disk_free_after_bytes=node.disk_free_after_bytes,
                memory_available_bytes=node.memory_available_bytes,
                memory_required_bytes=node.memory_required_bytes,
                memory_free_after_bytes=node.memory_free_after_bytes,
            )
            for node in group.nodes
        ]

    @staticmethod
    def _locations(group: Any | None) -> LibraryPlacementLocations:
        return LibraryPlacementLocations(
            installation_ids=list(group.installation_ids) if group is not None else [],
            run_ids=list(group.run_ids) if group is not None else [],
            installed=bool(group is not None and group.install_state == "complete"),
            running=bool(group is not None and group.load_state == "loaded"),
        )

    @staticmethod
    def _reason(
        code: str,
        detail: str,
        severity: str,
        node_ids: list[str],
    ) -> LibraryPlacementReason:
        return LibraryPlacementReason(
            code=code[:80],
            detail=detail[:512],
            severity=severity,
            node_ids=node_ids,
        )


__all__ = ["LibraryPlacementConflict", "LibraryPlacementService"]
