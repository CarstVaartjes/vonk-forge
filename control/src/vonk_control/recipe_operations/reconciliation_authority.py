"""Reconciliation authority for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, NoReturn
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    ReconcileCode,
    RouteState,
    RunState,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)

from .. import job_states
from ..admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    admission_attempts,
    lock_admission_rows,
)
from ..categorized_errors import (
    MissingRecord,
)
from ..install_admission import (
    InstallAdmissionBusy,
)
from ..models import (
    AgentNode,
    AgentOperation,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from ..recipe_action_plans import (
    UninstallPlan,
)
from ..recipe_progress import (
    _parent_reconciliation as _parent_reconciliation,  # noqa: PLC0414 -- shared helper export
)
from ..run_switch_contract import (
    RunSwitchReconciliationAuthority,
    RunSwitchReconciliationTarget,
)
from .constants import _MAX_ACTION_NODES
from .errors import RecipeReconciliationBlocked

if TYPE_CHECKING:
    from .service import RecipeOperationService


class ReconciliationAuthorityMixin:
    def preview_uninstall(
        self, installation_id: str, *, also_removing: Collection[str] = ()
    ) -> UninstallPlan:
        service = typing_cast("RecipeOperationService", self)
        last_error: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._preview_uninstall_once(
                    installation_id, also_removing=also_removing
                )
            except UnknownOutcomeError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _preview_uninstall_once(
        self, installation_id: str, *, also_removing: Collection[str] = ()
    ) -> UninstallPlan:
        """The uninstall plan; ``also_removing`` names installations removed in
        the same sweep, which do not count as users of the shared model files."""
        service = typing_cast("RecipeOperationService", self)

        with service._sessions() as session:
            return service._uninstall_plan_in_session(
                session, installation_id, lock=False, also_removing=also_removing
            )

    def preview_reconciliation_authority(
        self,
        installation_id: str,
        *,
        session: Session | None = None,
        allow_active_reconciliation: bool = False,
    ) -> RunSwitchReconciliationAuthority:
        """Bind an explicit cleanup review to accepted installation identity.

        This path reads the admitted installation document only as opaque JSON.
        It never converts the damaged launch specification into an executable
        plan and it does not weaken the ordinary uninstall assessment.
        """
        service = typing_cast("RecipeOperationService", self)

        if session is not None:
            return service._reconciliation_authority_in_session(
                session,
                installation_id,
                lock=False,
                allow_active_reconciliation=allow_active_reconciliation,
            )
        with service._sessions() as owned_session:
            return service._reconciliation_authority_in_session(
                owned_session,
                installation_id,
                lock=False,
                allow_active_reconciliation=allow_active_reconciliation,
            )

    def reconciliation_complete(
        self,
        request_id: str,
        *,
        expected_authority: RunSwitchReconciliationAuthority,
    ) -> bool:
        """Whether the reviewed reconciliation succeeded on every pending rank."""
        service = typing_cast("RecipeOperationService", self)

        with service._sessions() as session:
            job = session.scalar(
                select(Job).where(
                    Job.request_id == request_id,
                    Job.kind == WireAgentOperation.RECIPE_RECONCILE.value,
                )
            )
            if (
                job is None
                or job.state != LifecycleState.SUCCEEDED.value
                or canonical_message(_parent_reconciliation(job))
                != canonical_message(expected_authority)
            ):
                return False
            children = tuple(
                session.scalars(
                    select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
                )
            )
            return service._reconciliation_job_complete(session, job, children)

    @staticmethod
    def _lock_reconciliation_rows_in_session(
        session: Session, installation: RecipeInstallation
    ) -> None:
        """Acquire the complete reconciliation row set in canonical order."""

        node_rows = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation.id)
                .order_by(InstallationNode.rank, InstallationNode.node_id)
            )
        )
        node_ids = tuple(node.node_id for node in node_rows)
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == installation.mapping_id)
                .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
            )
        )
        runs = tuple(
            session.scalars(
                select(RecipeRun).where(RecipeRun.installation_id == installation.id)
            )
        )
        run_ids = tuple(run.id for run in runs)
        source_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == WireAgentOperation.RECIPE_INSTALL.value,
                    Job.state == LifecycleState.SUCCEEDED.value,
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                    Job.payload["plan_digest"].as_string() == installation.plan_digest,
                )
            )
        )
        reconciliation_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == WireAgentOperation.RECIPE_RECONCILE.value,
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                )
            )
        )
        active_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.OBSERVING,
                            LifecycleState.BACKOFF,
                        )
                    ),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                    Job.kind.in_(
                        (
                            WireAgentOperation.RECIPE_INSTALL.value,
                            WireAgentOperation.RECIPE_UNINSTALL.value,
                            WireAgentOperation.RECIPE_RECONCILE.value,
                        )
                    ),
                )
            )
        )
        active_run_jobs = (
            tuple(
                session.scalars(
                    select(Job).where(
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                        Job.payload["owner_kind"].as_string() == "run",
                        Job.payload["owner_id"].as_string().in_(run_ids),
                        Job.kind.in_(
                            (
                                WireAgentOperation.RECIPE_START.value,
                                WireAgentOperation.RECIPE_STOP.value,
                            )
                        ),
                    )
                )
            )
            if run_ids
            else ()
        )
        job_ids = tuple(
            sorted(
                {
                    job.id
                    for job in (
                        *source_jobs,
                        *reconciliation_jobs,
                        *active_jobs,
                        *active_run_jobs,
                    )
                }
            )
        )
        requests = (
            AdmissionRowLock(
                "reconcile-agent-nodes",
                AgentNode,
                select(AgentNode).where(AgentNode.node_id.in_(node_ids)),
            ),
            AdmissionRowLock(
                "reconcile-recipe-revision",
                CatalogDocumentRevision,
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == installation.recipe_revision_id
                ),
            ),
            AdmissionRowLock(
                "reconcile-mapping",
                ClusterMapping,
                select(ClusterMapping).where(
                    ClusterMapping.id == installation.mapping_id
                ),
            ),
            AdmissionRowLock(
                "reconcile-installation",
                RecipeInstallation,
                select(RecipeInstallation).where(
                    RecipeInstallation.id == installation.id
                ),
            ),
            AdmissionRowLock(
                "reconcile-mapping-nodes",
                ClusterMappingNode,
                select(ClusterMappingNode).where(
                    ClusterMappingNode.mapping_id == installation.mapping_id
                ),
            ),
            AdmissionRowLock(
                "reconcile-installation-nodes",
                InstallationNode,
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                ),
            ),
            AdmissionRowLock(
                "reconcile-jobs",
                Job,
                select(Job).where(Job.id.in_(job_ids)),
            ),
            AdmissionRowLock(
                "reconcile-agent-operations",
                AgentOperation,
                select(AgentOperation).where(AgentOperation.parent_job_id.in_(job_ids)),
            ),
        )
        try:
            locked = lock_admission_rows(session, requests)
        except AdmissionLockBusy as error:
            raise InstallAdmissionBusy(
                ReconcileCode.CAPACITY_BUSY, reason=WaitReason.OBSERVATION_UNAVAILABLE
            ) from error
        locked_nodes = locked["reconcile-installation-nodes"]
        locked_mapping_nodes = locked["reconcile-mapping-nodes"]
        if tuple(
            sorted((node.node_id, node.rank, node.role) for node in locked_nodes)
        ) != tuple(
            sorted((node.node_id, node.rank, node.role) for node in node_rows)
        ) or tuple(
            sorted(
                (node.node_id, node.rank, node.role) for node in locked_mapping_nodes
            )
        ) != tuple(
            sorted((node.node_id, node.rank, node.role) for node in mapping_nodes)
        ):
            raise InstallAdmissionBusy(
                ReconcileCode.MEMBERSHIP_CHANGED,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )

    def _reconciliation_authority_in_session(
        self,
        session: Session,
        installation_id: str,
        *,
        lock: bool,
        allow_active_reconciliation: bool = False,
    ) -> RunSwitchReconciliationAuthority:
        service = typing_cast("RecipeOperationService", self)
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == installation_id
        )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise MissingRecord(installation_id)

        if lock:
            service._lock_reconciliation_rows_in_session(session, installation)
            # The helper refreshed every locked row after acquiring the full
            # set in canonical table/primary-key order. Keep later reads free
            # of ad-hoc locks that could invert that order.
            lock = False
            allow_active_reconciliation = False

        def blocked(code: str, detail: str) -> NoReturn:
            raise RecipeReconciliationBlocked(code, detail)

        revision_statement = select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.id == installation.recipe_revision_id,
            CatalogDocumentRevision.kind == "recipe",
        )
        if lock:
            revision_statement = revision_statement.with_for_update(
                of=CatalogDocumentRevision
            )
        revision = session.scalar(revision_statement)
        if revision is None or revision.content_digest is None:
            blocked(
                ReconcileCode.RECIPE_REVISION_UNAVAILABLE,
                "The installation's exact accepted recipe revision is unavailable.",
            )

        # Cleanup binds the accepted relational identity. Launch JSON is a
        # projection that may no longer parse after upgrade; the helper still
        # checks this installation's exact plan digest against local effects.
        if installation.state not in {
            InstallationState.INSTALLED,
            InstallationState.PARTIAL,
            InstallationState.FAILED,
        }:
            blocked(
                ReconcileCode.INSTALLATION_EFFECT_UNKNOWN,
                f"Installation state {installation.state} does not prove a complete installed effect.",
            )

        node_statement = (
            select(InstallationNode)
            .where(InstallationNode.installation_id == installation.id)
            .order_by(InstallationNode.rank, InstallationNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=InstallationNode)
        all_nodes = tuple(session.scalars(node_statement))
        if not all_nodes or len(all_nodes) > _MAX_ACTION_NODES:
            blocked(
                ReconcileCode.RANK_MEMBERSHIP_CHANGED,
                "The installation has no bounded exact node membership.",
            )
        mapping_statement = select(ClusterMapping).where(
            ClusterMapping.id == installation.mapping_id
        )
        if lock:
            mapping_statement = mapping_statement.with_for_update(of=ClusterMapping)
        mapping = session.scalar(mapping_statement)
        mapping_nodes_statement = (
            select(ClusterMappingNode)
            .where(ClusterMappingNode.mapping_id == installation.mapping_id)
            .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
        )
        if lock:
            mapping_nodes_statement = mapping_nodes_statement.with_for_update(
                of=ClusterMappingNode
            )
        mapping_nodes = tuple(session.scalars(mapping_nodes_statement))
        actual_membership = tuple(
            (node.node_id, node.rank, node.role) for node in all_nodes
        )
        mapping_membership = tuple(
            (node.node_id, node.rank, node.role) for node in mapping_nodes
        )
        if (
            mapping is None
            or mapping.recipe_revision_id != revision.id
            or mapping.node_count != len(mapping_nodes)
            or not mapping_nodes
            or actual_membership != mapping_membership
            or tuple(node.rank for node in all_nodes) != tuple(range(len(all_nodes)))
            or mapping.endpoint_owner_node_id
            not in {node.node_id for node in mapping_nodes}
        ):
            blocked(
                ReconcileCode.RANK_MEMBERSHIP_CHANGED,
                "The stored installation, relational membership, and saved mapping no longer identify the same exact ranks.",
            )

        active_runs_statement = select(RecipeRun).where(
            RecipeRun.installation_id == installation.id
        )
        if lock:
            active_runs_statement = active_runs_statement.with_for_update(of=RecipeRun)
        installation_runs = tuple(session.scalars(active_runs_statement))
        if any(
            run.state != RunState.STOPPED or run.route_state != RouteState.WITHDRAWN
            for run in installation_runs
        ):
            blocked(
                ReconcileCode.ACTIVE_EFFECT_UNKNOWN,
                "An installation run or route is active or has an unconfirmed effect.",
            )

        active_jobs_statement = select(Job).where(
            Job.state.in_(
                job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.OBSERVING,
                    LifecycleState.BACKOFF,
                )
            ),
            Job.payload["owner_kind"].as_string() == "installation",
            Job.payload["owner_id"].as_string() == installation.id,
            Job.kind.in_(
                (
                    WireAgentOperation.RECIPE_INSTALL.value,
                    WireAgentOperation.RECIPE_UNINSTALL.value,
                    WireAgentOperation.RECIPE_RECONCILE.value,
                )
            ),
        )
        if lock:
            active_jobs_statement = active_jobs_statement.with_for_update(of=Job)
        active_jobs = tuple(session.scalars(active_jobs_statement))
        run_ids = {run.id for run in installation_runs}
        active_run_jobs = (
            tuple(
                session.scalars(
                    select(Job).where(
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                        Job.payload["owner_kind"].as_string() == "run",
                        Job.payload["owner_id"].as_string().in_(run_ids),
                        Job.kind.in_(
                            (
                                WireAgentOperation.RECIPE_START.value,
                                WireAgentOperation.RECIPE_STOP.value,
                            )
                        ),
                    )
                )
            )
            if run_ids
            else ()
        )
        blocking_active_jobs = (
            tuple(
                job
                for job in active_jobs
                if job.kind != WireAgentOperation.RECIPE_RECONCILE.value
            )
            if allow_active_reconciliation
            else active_jobs
        )
        if blocking_active_jobs or active_run_jobs:
            blocked(
                ReconcileCode.OPERATION_ACTIVE,
                "An install, start, stop, uninstall, or reconciliation operation is still active or uncertain.",
            )

        node_by_id = {node.node_id: node for node in all_nodes}
        agent_nodes_statement = (
            select(AgentNode)
            .where(AgentNode.node_id.in_(node_by_id))
            .order_by(AgentNode.node_id)
        )
        if lock:
            agent_nodes_statement = agent_nodes_statement.with_for_update(of=AgentNode)
        agent_nodes = tuple(session.scalars(agent_nodes_statement))
        agent_node_by_id = {node.node_id: node for node in agent_nodes}
        targets: list[RunSwitchReconciliationTarget] = []
        for node in all_nodes:
            agent_node = agent_node_by_id.get(node.node_id)
            if (
                agent_node is None
                or agent_node.state != "active"
                or agent_node.revoked_at is not None
            ):
                blocked(
                    ReconcileCode.AGENT_UNAVAILABLE,
                    f"Node {node.node_id} is not an active authorized target.",
                )
            # A succeeded, fenced reconcile attempt marked the rank uninstalled;
            # that state is the proof its cleanup already happened.
            targets.append(
                RunSwitchReconciliationTarget(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    installed_bytes=node.installed_bytes,
                    state="reconciled"
                    if node.state == InstallationNodeState.UNINSTALLED
                    else "pending",
                )
            )

        return RunSwitchReconciliationAuthority(
            installation_id=installation.id,
            original_plan_digest=installation.plan_digest,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            mapping_id=mapping.id,
            mapping_generation=installation.mapping_generation,
            recipe_build_id=installation.recipe_build_id,
            image_digest=installation.image_digest,
            model_content_sha256=installation.model_content_sha256,
            targets=targets,
        )
