"""Assignment assessment for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    ObservedAssignmentState,
    RouteState,
    RunState,
)
from vonk_agent_protocol.agent_words import ProfileDocumentState

from ..cluster_mappings import mapping_option_choices
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
from .dependencies import _ACTIVE_INSTALL_STATES
from .persistence import _stored_recipe
from .projection_support import (
    _effective_option_choices,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


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

    @classmethod
    def _assignment_state(
        cls,
        session: Session,
        assignment: FleetProfileAssignment,
        *,
        expected_image: RuntimeImageIdentity | None = None,
    ) -> _AssignmentState:
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface
        build = (
            (
                session.get(RecipeBuild, expected_image.build_id)
                if expected_image is not None and expected_image.build_id is not None
                else None
            )
            if expected_image is not None
            else session.scalar(
                select(RecipeBuild)
                .where(
                    RecipeBuild.recipe_revision_id == assignment.recipe_revision_id,
                    RecipeBuild.state == LifecycleState.SUCCEEDED.value,
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
                .limit(1)
            )
        )
        mappings = tuple(
            session.scalars(
                select(ClusterMapping)
                .where(
                    ClusterMapping.recipe_revision_id == assignment.recipe_revision_id,
                    ClusterMapping.topology_name == assignment.topology_name,
                    ClusterMapping.state == ProfileDocumentState.READY.value,
                )
                .order_by(ClusterMapping.updated_at.desc(), ClusterMapping.id.desc())
            )
        )
        expected = {
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in assignment.nodes
        }
        recipe_revision = session.get(
            CatalogDocumentRevision, assignment.recipe_revision_id
        )
        # An installation and run made with other option choices are not this
        # assignment's, even for the same recipe revision and Sparks.
        wanted_choices = (
            _effective_option_choices(
                _stored_recipe(recipe_revision),
                assignment.option_choices,
            )[0]
            if recipe_revision is not None
            else dict(assignment.option_choices)
        )
        mapping = None
        for candidate in mappings:
            if mapping_option_choices(candidate.parameters) != wanted_choices:
                continue
            members = tuple(
                session.scalars(
                    select(ClusterMappingNode).where(
                        ClusterMappingNode.mapping_id == candidate.id
                    )
                )
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
                for candidate in session.scalars(
                    select(RecipeInstallation)
                    .where(
                        RecipeInstallation.mapping_id == mapping.id,
                        RecipeInstallation.recipe_revision_id
                        == assignment.recipe_revision_id,
                        RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES),
                    )
                    .order_by(
                        RecipeInstallation.updated_at.desc(),
                        RecipeInstallation.id.desc(),
                    )
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
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                )
            )
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
        run = session.scalar(
            select(RecipeRun)
            .where(
                RecipeRun.installation_id == installation.id,
                RecipeRun.state.in_(STOPPABLE_RUN_STATES),
            )
            .order_by(RecipeRun.updated_at.desc(), RecipeRun.id.desc())
            .limit(1)
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
        run_members = tuple(
            session.scalars(select(RunNode).where(RunNode.run_id == run.id))
        )
        live_run_node_ids = set(
            session.scalars(
                select(AgentNode.node_id).where(
                    AgentNode.node_id.in_(tuple(node.node_id for node in run_members)),
                    AgentNode.revoked_at.is_(None),
                )
            )
        )
        healthy = (
            run.state == RunState.RUNNING
            and run.route_state == RouteState.PUBLISHED
            and len(run_members) == len(expected)
            and live_run_node_ids == {node.node_id for node in run_members}
            and {(node.node_id, node.rank, node.role) for node in run_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(node.state == RunState.RUNNING for node in run_members)
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
