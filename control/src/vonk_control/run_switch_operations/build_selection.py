"""Build selection."""

from __future__ import annotations

from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RunSwitchCode,
    SecurityRefusalError,
    UnknownOutcomeError,
)

from ..inventory_items import runtime_value
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    RecipeBuild,
)
from ..recipe_operations import (
    RecipeOperationConflict,
)
from ..revision_images import revision_source_bundle
from ..run_switch_contract import (
    FreshnessEvidence,
    SparkGroup,
)
from .identity_helpers import _is_hex_digest
from .interfaces import _BuildSelection
from .planning_helpers import PlanInventory, _as_reason

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class BuildSelectionMixin:
    def _select_build(
        self,
        session: Session,
        revision: CatalogDocumentRevision,
        build: RecipeBuild | None,
        candidate: RecipeBuild | None,
        group: SparkGroup,
        *,
        now: datetime,
        inventory: PlanInventory | None = None,
        create_build: bool = True,
    ) -> _BuildSelection:
        """Resolve an immutable build or create a pending Controller build.

        A successful receipt is reusable without re-admitting its builder.  A
        pending receipt is reusable only while its builder still has fresh
        typed build evidence.  Otherwise the Controller chooses the first
        compatible builder in deterministic order, preferring a node outside
        the inference group, and delegates planning to the existing recipe
        build primitive.  This method only creates the durable *planned*
        receipt; bytes are produced by ``recipe.build.v1`` during apply.
        """
        service = typing_cast("RunSwitchOperationService", self)
        if inventory is None:
            inventory = PlanInventory(
                session, now=now, maximum_age_seconds=service._inventory_max_age
            )

        if build is not None:
            return _BuildSelection(build=build, candidate=build)

        group_ids = {node.node_id for node in group.nodes}
        source = revision_source_bundle(revision)
        if (
            candidate is not None
            and candidate.state in {"planned", "building"}
            # A pending build of a superseded source bundle builds another image.
            and source in {None, candidate.source_bundle_sha256}
        ):
            builder = session.get(AgentNode, candidate.builder_node_id)
            freshness, admissible = service._builder_admission(
                session, builder, now=now, inventory=inventory
            )
            if admissible:
                return _BuildSelection(
                    build=None,
                    candidate=candidate,
                    builder_freshness=freshness,
                )

        # The same executable inputs mean the same image, whichever revision
        # first built it: reuse that build by content, creating nothing.
        reusable_build_id = getattr(service._lifecycle, "reusable_build_id", None)
        if callable(reusable_build_id):
            found_id = reusable_build_id(revision.id)
            found = session.get(RecipeBuild, found_id) if found_id else None
            if found is not None:
                # The lookup may observe completion in another transaction;
                # this Session can still hold the earlier unfinished row.
                session.refresh(found)
            if found is not None and service._build_is_available(found):
                return _BuildSelection(build=found, candidate=found)

        preview_build = getattr(service._lifecycle, "preview_build", None)
        if not create_build:
            # The same missing-build evidence below explains the blocker.
            # Inspection is not permission to persist preparation intent.
            return _BuildSelection(build=None, candidate=candidate)
        if not callable(preview_build):
            return _BuildSelection(
                build=None,
                candidate=None,
                blockers=(
                    _as_reason(
                        RunSwitchCode.CONTAINER_BUILD_UNAVAILABLE,
                        "The existing recipe build primitive is unavailable; the Controller cannot prepare the exact OCI runtime image.",
                        scope="operation",
                        node_ids=[node.node_id for node in group.nodes],
                    ),
                ),
            )

        ordered_nodes = service._builder_nodes(session, group_ids)
        errors: list[str] = []
        saw_builder = False
        for node in ordered_nodes:
            freshness, admissible = service._builder_admission(
                session, node, now=now, inventory=inventory
            )
            if not admissible:
                continue
            saw_builder = True
            try:
                proposed = preview_build(revision.id, node.node_id)
            except SecurityRefusalError:
                raise
            except UnknownOutcomeError as error:
                # End this preview without selecting a different effect on
                # unknown evidence. An accepted blocked plan is refreshed by
                # the worker with bounded backoff, using this same revision.
                return _BuildSelection(
                    build=None,
                    candidate=None,
                    blockers=(
                        _as_reason(
                            RunSwitchCode.CONTAINER_BUILD_UNAVAILABLE,
                            f"Waiting for exact builder evidence: {error}",
                            scope="operation",
                            node_ids=[node.node_id for node in group.nodes],
                        ),
                    ),
                )
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                errors.append(f"{node.node_id}: {error}")
                continue
            proposed_id = getattr(proposed, "build_id", None)
            if not isinstance(proposed_id, str):
                errors.append(
                    f"{node.node_id}: build preview returned no build identity"
                )
                continue
            selected = session.get(RecipeBuild, proposed_id)
            if selected is not None:
                # ``preview_build`` persists through the lifecycle service's
                # own short transaction.  This Session may already hold the
                # succeeded row that was reset after its archive disappeared;
                # refresh it so the first preview sees the durable planned row.
                session.refresh(selected)
            if selected is None:
                errors.append(f"{node.node_id}: build preview receipt is unavailable")
                continue
            # preview_build can finish or repair a build in its own transaction.
            # Decide from the refreshed content receipt, not its revision.
            if (
                getattr(proposed, "build_input_sha256", None)
                != selected.build_input_sha256
            ):
                errors.append(f"{node.node_id}: build preview receipt is unavailable")
                continue
            if service._build_is_available(selected):
                return _BuildSelection(build=selected, candidate=selected)
            if selected.state != LifecycleState.SUCCEEDED:
                return _BuildSelection(
                    build=None,
                    candidate=selected,
                    builder_freshness=freshness,
                )
            errors.append(f"{node.node_id}: build preview receipt is unavailable")

        if saw_builder:
            detail = "No compatible Controller builder could prepare the exact recipe source and runtime image."
            if errors:
                detail += " " + errors[-1]
        else:
            detail = "No active linux-arm64 worker has fresh recipe.build.v1 admission evidence."
        return _BuildSelection(
            build=None,
            candidate=None,
            blockers=(
                _as_reason(
                    RunSwitchCode.CONTAINER_BUILD_UNAVAILABLE,
                    detail,
                    scope="operation",
                    node_ids=[node.node_id for node in group.nodes],
                ),
            ),
        )

    @staticmethod
    def _builder_nodes(session: Session, group_ids: set[str]) -> tuple[AgentNode, ...]:
        nodes = tuple(
            session.scalars(
                select(AgentNode)
                .where(
                    AgentNode.state == "active",
                    AgentNode.revoked_at.is_(None),
                )
                .order_by(AgentNode.node_id)
            )
        )
        # A builder may be a member of the selected group, but a separate
        # active worker is preferred so build memory cannot contend with the
        # inference admission.  Both choices remain deterministic.
        return tuple(
            sorted(
                (
                    node
                    for node in nodes
                    if runtime_value(node, lambda identity: identity.architecture)
                    == "linux-arm64"
                ),
                key=lambda node: (node.node_id in group_ids, node.node_id),
            )
        )

    def _builder_admission(
        self,
        session: Session,
        node: AgentNode | None,
        *,
        now: datetime,
        inventory: PlanInventory | None = None,
    ) -> tuple[FreshnessEvidence | None, bool]:
        """Return fresh builder evidence and whether it can run recipe.build.v1."""
        service = typing_cast("RunSwitchOperationService", self)
        if inventory is None:
            inventory = PlanInventory(
                session, now=now, maximum_age_seconds=service._inventory_max_age
            )

        if node is None:
            return None, False
        snapshot, freshness = inventory.read(node.node_id)
        if (
            node.state != "active"
            or node.revoked_at is not None
            or runtime_value(node, lambda identity: identity.architecture)
            != "linux-arm64"
            or not _is_hex_digest(
                runtime_value(node, lambda identity: identity.binary_digest)
            )
            or freshness.state != "fresh"
        ):
            return freshness, False
        if snapshot is None or "recipe.build.v1" not in snapshot.capabilities:
            return freshness, False
        return freshness, True
