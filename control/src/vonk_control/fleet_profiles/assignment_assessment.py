"""Assignment assessment for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
from operator import attrgetter
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    ObservedAssignmentState,
)
from vonk_agent_protocol.agent_words import ProfileDocumentState
from vonk_forge_contracts import RecipeDefinition

from ..cluster_mappings import mapping_option_choices
from ..content_identity import catalog_reference_membership
from ..fleet_profile_contract import FleetProfileAssignment, FleetProfileAssignmentState
from ..models import (
    STOPPABLE_RUN_STATES,
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..preparation_contract import RuntimeImageIdentity
from ..recipe_execution_contract import (
    installation_matches_runtime_image,
    installation_serves_authorised_ports,
)
from ..recipe_operations import _run_is_one_shot
from ..run_readiness import run_not_ready_for_profile
from .dependencies import _ACTIVE_INSTALL_STATES
from .persistence import _stored_recipe
from .projection_support import (
    _effective_option_choices,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


@dataclass
class _AssignmentRows:
    builds: dict[str, tuple[RecipeBuild, ...]]
    mappings: dict[tuple[str, str], tuple[ClusterMapping, ...]]
    mapping_nodes: tuple[ClusterMappingNode, ...]
    installations: dict[tuple[str, str], tuple[RecipeInstallation, ...]]
    installation_nodes: tuple[InstallationNode, ...]
    runs: tuple[RecipeRun, ...]
    run_nodes: tuple[RunNode, ...]
    live_node_ids: set[str]
    revisions: dict[str, CatalogDocumentRevision]


class FleetProfileService:
    class _AssignmentState:
        current_state: FleetProfileAssignmentState
        mapping: ClusterMapping | None
        installation: RecipeInstallation | None
        run: RecipeRun | None
        build: RecipeBuild | None
        installation_ready: bool

        def __init__(
            self,
            *,
            current_state: FleetProfileAssignmentState,
            mapping: ClusterMapping | None,
            installation: RecipeInstallation | None,
            run: RecipeRun | None,
            build: RecipeBuild | None,
            installation_ready: bool = False,
        ) -> None:
            self.current_state = current_state
            self.mapping = mapping
            self.installation = installation
            self.run = run
            self.build = build
            self.installation_ready = installation_ready

    @staticmethod
    def _assignment_rows(
        session: Session, assignments: Sequence[FleetProfileAssignment]
    ) -> _AssignmentRows:
        """Read live state once for the distinct revisions and their exact effects."""
        revisions = {item.recipe_revision_id for item in assignments}
        documents = (
            {
                item.id: item
                for item in session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.id.in_(revisions)
                    )
                )
            }
            if revisions
            else {}
        )
        builds = tuple(
            session.scalars(
                select(RecipeBuild)
                .where(
                    catalog_reference_membership(
                        RecipeBuild.recipe_revision_id, revisions
                    ),
                    RecipeBuild.state == LifecycleState.SUCCEEDED.value,
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
            )
        )
        mappings = tuple(
            session.scalars(
                select(ClusterMapping)
                .where(
                    catalog_reference_membership(
                        ClusterMapping.recipe_revision_id, revisions
                    ),
                    ClusterMapping.state == ProfileDocumentState.READY.value,
                )
                .order_by(ClusterMapping.updated_at.desc(), ClusterMapping.id.desc())
            )
        )
        mapping_ids = [item.id for item in mappings]
        mapping_nodes = (
            tuple(
                session.scalars(
                    select(ClusterMappingNode).where(
                        ClusterMappingNode.mapping_id.in_(mapping_ids)
                    )
                )
            )
            if mappings
            else ()
        )
        installations = (
            tuple(
                session.scalars(
                    select(RecipeInstallation)
                    .where(
                        RecipeInstallation.mapping_id.in_(mapping_ids),
                        catalog_reference_membership(
                            RecipeInstallation.recipe_revision_id, revisions
                        ),
                        RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES),
                    )
                    .order_by(
                        RecipeInstallation.updated_at.desc(),
                        RecipeInstallation.id.desc(),
                    )
                )
            )
            if mappings
            else ()
        )
        installation_ids = [item.id for item in installations]
        installation_nodes = (
            tuple(
                session.scalars(
                    select(InstallationNode).where(
                        InstallationNode.installation_id.in_(installation_ids)
                    )
                )
            )
            if installations
            else ()
        )
        runs = (
            tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.installation_id.in_(installation_ids),
                        RecipeRun.state.in_(STOPPABLE_RUN_STATES),
                    )
                    .order_by(RecipeRun.updated_at.desc(), RecipeRun.id.desc())
                )
            )
            if installations
            else ()
        )
        run_nodes = (
            tuple(
                session.scalars(
                    select(RunNode).where(
                        RunNode.run_id.in_([item.id for item in runs])
                    )
                )
            )
            if runs
            else ()
        )
        live = (
            set(
                session.scalars(
                    select(AgentNode.node_id).where(
                        AgentNode.node_id.in_({item.node_id for item in run_nodes}),
                        AgentNode.revoked_at.is_(None),
                    )
                )
            )
            if run_nodes
            else set()
        )
        return _AssignmentRows(
            {
                key: tuple(items)
                for key, items in groupby(
                    sorted(builds, key=attrgetter("recipe_revision_id")),
                    attrgetter("recipe_revision_id"),
                )
            },
            {
                key: tuple(items)
                for key, items in groupby(
                    sorted(
                        mappings, key=attrgetter("recipe_revision_id", "topology_name")
                    ),
                    attrgetter("recipe_revision_id", "topology_name"),
                )
            },
            mapping_nodes,
            {
                key: tuple(items)
                for key, items in groupby(
                    sorted(
                        installations,
                        key=attrgetter("mapping_id", "recipe_revision_id"),
                    ),
                    attrgetter("mapping_id", "recipe_revision_id"),
                )
            },
            installation_nodes,
            runs,
            run_nodes,
            live,
            documents,
        )

    @classmethod
    def _assignment_state(
        cls,
        session: Session,
        assignment: FleetProfileAssignment,
        *,
        expected_image: RuntimeImageIdentity | None = None,
        rows: _AssignmentRows | None = None,
    ) -> _AssignmentState:
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface
        rows = rows if rows is not None else cls._assignment_rows(session, [assignment])
        build = (
            (
                session.get(RecipeBuild, expected_image.build_id)
                if expected_image is not None and expected_image.build_id is not None
                else None
            )
            if expected_image is not None
            else next(
                (
                    item
                    for item in rows.builds.get(assignment.recipe_revision_id, ())
                    if item.state == LifecycleState.SUCCEEDED.value
                ),
                None,
            )
        )
        mappings = rows.mappings.get(
            (assignment.recipe_revision_id, assignment.topology_name), ()
        )
        expected = {
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in assignment.nodes
        }
        recipe_revision = rows.revisions.get(assignment.recipe_revision_id)
        recipe = _stored_recipe(recipe_revision) if recipe_revision else None
        # An installation and run made with other option choices are not this
        # assignment's, even for the same recipe revision and Sparks.  A revision
        # that is gone or no longer readable is matched by the choices the
        # assignment stored, so its serving run is still observed as serving.
        wanted_choices = (
            _effective_option_choices(recipe, assignment.option_choices)[0]
            if isinstance(recipe, RecipeDefinition)
            else dict(assignment.option_choices)
        )
        mapping = None
        for candidate in mappings:
            if mapping_option_choices(candidate.parameters) != wanted_choices:
                continue
            members = (
                item for item in rows.mapping_nodes if item.mapping_id == candidate.id
            )
            actual = {
                (node.node_id, node.rank, node.role, node.endpoint_owner)
                for node in members
            }
            if actual == expected:
                mapping = candidate
                break
        if mapping is None:
            return cls._AssignmentState(
                current_state=ObservedAssignmentState.NOT_PLACED,
                mapping=None,
                installation=None,
                run=None,
                build=build,
            )
        installation = next(
            (
                candidate
                for candidate in rows.installations.get(
                    (mapping.id, assignment.recipe_revision_id), ()
                )
                # An installation compiled for a port the platform no longer
                # assigns cannot launch; the load installs the recipe again.
                if installation_serves_authorised_ports(candidate)
            ),
            None,
        )
        if installation is None:
            return cls._AssignmentState(
                current_state=ObservedAssignmentState.PLACED,
                mapping=mapping,
                installation=None,
                run=None,
                build=build,
            )
        install_members = tuple(
            item
            for item in rows.installation_nodes
            if item.installation_id == installation.id
        )
        exact_installed = (
            installation.state == InstallationState.INSTALLED
            and len(install_members) == len(expected)
            and {(node.node_id, node.rank, node.role) for node in install_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(
                node.state == InstallationNodeState.INSTALLED
                for node in install_members
            )
            and (
                installation_matches_runtime_image(
                    installation,
                    image_digest=expected_image.image_digest,
                    oci_layout_sha256=expected_image.oci_layout_sha256,
                    image_bytes=expected_image.image_bytes,
                )
                if expected_image is not None
                else (
                    build is None
                    or installation_matches_runtime_image(
                        installation,
                        image_digest=build.image_digest,
                        oci_layout_sha256=build.oci_layout_sha256,
                        image_bytes=build.image_bytes,
                    )
                )
            )
        )
        if not exact_installed:
            state: FleetProfileAssignmentState = (
                ObservedAssignmentState.INSTALLING
                if installation.state
                in {InstallationState.PLANNED, InstallationState.INSTALLING}
                else ObservedAssignmentState.DEGRADED
            )
            return cls._AssignmentState(
                current_state=state,
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
            )
        run = next(
            (item for item in rows.runs if item.installation_id == installation.id),
            None,
        )
        if run is None:
            return cls._AssignmentState(
                current_state=ObservedAssignmentState.INSTALLED,
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
                installation_ready=True,
            )
        run_members = tuple(item for item in rows.run_nodes if item.run_id == run.id)
        live_run_node_ids = rows.live_node_ids & {item.node_id for item in run_members}
        healthy = (
            len(run_members) == len(expected)
            and live_run_node_ids == {node.node_id for node in run_members}
            and {(node.node_id, node.rank, node.role) for node in run_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and run_not_ready_for_profile(
                one_shot=_run_is_one_shot(session, run),
                state=run.state,
                route_state=run.route_state,
                rank_states=(node.state for node in run_members),
            )
            is None
        )
        return cls._AssignmentState(
            current_state=(
                ObservedAssignmentState.RUNNING
                if healthy
                else ObservedAssignmentState.DEGRADED
            ),
            mapping=mapping,
            installation=installation,
            run=run,
            build=build,
            installation_ready=True,
        )

    @staticmethod
    def _installation_nodes(
        session: Session, installation_ids: Sequence[str]
    ) -> dict[str, tuple[InstallationNode, ...]]:
        grouped: dict[str, list[InstallationNode]] = {}
        if installation_ids:
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id.in_(installation_ids)
                )
            ):
                grouped.setdefault(node.installation_id, []).append(node)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _run_nodes(
        session: Session, run_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        if run_ids:
            for node in session.scalars(
                select(RunNode).where(RunNode.run_id.in_(run_ids))
            ):
                grouped.setdefault(node.run_id, []).append(node.node_id)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _installation_node_ids(
        session: Session, installation_id: str
    ) -> tuple[str, ...]:
        return tuple(
            node.node_id
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
