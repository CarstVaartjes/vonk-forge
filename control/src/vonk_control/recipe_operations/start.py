"""Start for digest-bound recipe operations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RecipeStartPayload,
    RunState,
    UnknownOutcomeError,
    canonical_message,
)

from .. import job_states
from ..admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    admission_attempts,
    admission_wait_exhausted,
    job_request_key,
    node_admission_key,
)
from ..categorized_errors import (
    MissingRecord,
)
from ..lifecycle.evidence import (
    Residue,
)
from ..models import (
    AgentPresence,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..recipe_progress import (
    _canonical_distributed_readiness as _canonical_distributed_readiness,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _role_phases as _role_phases,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _topology_order as _topology_order,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_runtime_specs import recipe_topology
from ..recipe_start_payloads import (
    RecipeStartPayloadError,
    RecipeStartPlacement,
    build_recipe_start_payload,
)
from ..run_admission import (
    RunAdmissionBusy,
    RunNodePlan,
    RunPlan,
)
from ..run_admission import (
    require_admissible as require_run_admissible,
)
from ..run_admission import (
    require_same_execution as require_same_run_execution,
)
from ..stored_json import read_row_column
from .errors import RecipeRequestInvalid, RecipeRetryLater
from .intent import _run_start_intent
from .interfaces import RecipeOperationView
from .observation_helpers import _active_recipe_revision, _aware

if TYPE_CHECKING:
    from .service import RecipeOperationService


class StartMixin:
    def _start_once(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        if plan_digest != plan.plan_digest:
            raise RecipeRequestInvalid(
                "reviewed plan digest does not match the submitted plan",
                reason=InvalidRequestReason.CONFLICT,
            )
        reviewed = plan
        existing = service._idempotent(
            request_id,
            WireAgentOperation.RECIPE_START.value,
            None,
            installation_id=plan.installation_id,
            run_alias=plan.alias,
        )
        if existing is not None:
            return existing
        now = service._clock()
        with service._sessions.begin() as session:
            plan = service._run_admission.plan_run(
                plan.installation_id,
                plan.alias,
                now=now,
                _session=session,
                profile_application_id=profile_application_id,
            )
            require_run_admissible(plan)
            require_same_run_execution(reviewed, plan)
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        *(node_admission_key(node.node_id) for node in plan.nodes),
                    ),
                    holder="recipe-operation",
                )
            except AdmissionLockBusy as error:
                raise RunAdmissionBusy("run capacity writer is busy") from error
            replay = service._idempotent_in_session(
                session,
                request_id,
                WireAgentOperation.RECIPE_START.value,
                None,
                installation_id=plan.installation_id,
                run_alias=plan.alias,
            )
            if replay is not None:
                return replay
            presences = {
                node_id: address
                for node_id, address in session.execute(
                    select(
                        AgentPresence.node_id, AgentPresence.management_address
                    ).where(
                        AgentPresence.node_id.in_([node.node_id for node in plan.nodes])
                    )
                )
            }
            if set(presences) != {node.node_id for node in plan.nodes}:
                # A Spark reports its address when it next contacts the Controller.
                raise RecipeRetryLater("recipe node endpoint evidence is unavailable")
            master = next((node for node in plan.nodes if node.endpoint_owner), None)
            if master is None:
                raise RecipeRequestInvalid("recipe run has no endpoint owner")
            world_size = len(plan.nodes)
            master_address = master.fabric_address if world_size > 1 else None
            master_port = master.rendezvous_port if world_size > 1 else None
            if world_size > 1 and (master_address is None or master_port is None):
                raise RecipeRequestInvalid(
                    "recipe direct-fabric rendezvous is unavailable"
                )
            active_uninstall = session.scalar(
                select(Job.id)
                .where(
                    Job.kind.in_(
                        (
                            WireAgentOperation.RECIPE_UNINSTALL.value,
                            WireAgentOperation.RECIPE_RECONCILE.value,
                        )
                    ),
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == plan.installation_id,
                )
                .limit(1)
            )
            if active_uninstall is not None:
                raise RecipeRetryLater("recipe installation is not runnable")
            try:
                run_id = service._run_admission.accept_run_in_session(
                    session,
                    plan,
                    actor=actor,
                    now=now,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except RunAdmissionBusy:
                raise
            except (RuntimeError, ValueError) as error:
                raise RecipeRequestInvalid(str(error)) from error
            run = session.get(RecipeRun, run_id)
            revision = _active_recipe_revision(session, plan.recipe_revision_id)
            installation = session.get(RecipeInstallation, plan.installation_id)
            assert run is not None and revision is not None and installation is not None
            loaded_plans = service._stored_compiled_plans(
                session, installation, [node.node_id for node in plan.nodes], now=now
            )
            if isinstance(loaded_plans, Residue):
                raise RecipeRetryLater(
                    "compiled execution plan is unavailable for the installed recipe; "
                    "it is recorded and the next preparation installs afresh"
                )
            compiled_plans = loaded_plans
            start_order = _topology_order(
                read_row_column(revision, "document"), "start_order"
            )
            if start_order is None:
                raise RecipeRequestInvalid("recipe topology is invalid")
            topology = recipe_topology(read_row_column(revision, "document"))
            distributed_readiness = _canonical_distributed_readiness(
                read_row_column(revision, "document")
            )
            two_phase_start = (
                world_size > 1 and topology.distributed and distributed_readiness
            )
            start_deadline = (
                # Persist the accepted loading/JIT budget. Exact rank-loss recovery
                # derives this same duration; neither lease renewal nor retry
                # extends a deadline.
                (
                    _aware(now)
                    + timedelta(seconds=service._distributed_start_timeout_seconds)
                ).isoformat()
                if two_phase_start
                else None
            )
            run.state = RunState.STARTING
            run.updated_at = now
            recipe_digest = revision.content_digest
            assert recipe_digest is not None

            def start_payload(node: RunNodePlan) -> tuple[str, RecipeStartPayload]:
                endpoint_owner = node.endpoint_owner
                node_id = node.node_id
                try:
                    endpoint_address = (
                        presences[node_id] if endpoint_owner else node.fabric_address
                    )
                    if not isinstance(endpoint_address, str):
                        raise MissingRecord(
                            "recipe start endpoint address is unavailable"
                        )
                    payload = build_recipe_start_payload(
                        run_id=run_id,
                        installation_id=plan.installation_id,
                        recipe_revision_id=plan.recipe_revision_id,
                        mapping_id=run.mapping_id,
                        run_generation=run.run_generation,
                        plan_digest=plan.plan_digest,
                        placement=RecipeStartPlacement(
                            node_id,
                            node.rank,
                            node.role,
                            node.port,
                            node.required_memory_bytes,
                            node.memory_floor_bytes,
                            node.memory_kind,
                            node.fabric_address,
                        ),
                        compiled_endpoint_address=(
                            presences[node_id] if endpoint_owner else None
                        ),
                        world_size=world_size,
                        compiled_execution_plan=compiled_plans[node_id],
                        master_address=master_address,
                        master_port=master_port,
                        phase="rank-launch" if start_deadline is not None else None,
                        start_deadline=start_deadline,
                    )
                except (KeyError, RecipeStartPayloadError) as error:
                    raise RecipeRequestInvalid(
                        "recipe start payload is invalid"
                    ) from error
                return node_id, RecipeStartPayload.model_validate_json(
                    canonical_message(payload)
                )

            start_payloads = tuple(
                (node_id, json.loads(canonical_message(payload)))
                for node_id, payload in (start_payload(node) for node in plan.nodes)
            )
            role_phases = _role_phases(start_order, start_payloads)
            if role_phases is None:
                # Starting blind (without the recipe's role order) could launch
                # a rank before what it depends on.
                raise RecipeRequestInvalid("operation topology order is invalid")
            phases = role_phases
            if start_deadline is not None:
                owner_payload = next(
                    payload
                    for node_id, payload in start_payloads
                    if node_id == master.node_id
                )
                phases = (
                    tuple(item for phase in role_phases for item in phase),
                    (
                        (
                            master.node_id,
                            {
                                **owner_payload,
                                "phase": "collective-readiness",
                            },
                        ),
                    ),
                )
            job = service._queue_in_session(
                session,
                kind=WireAgentOperation.RECIPE_START.value,
                owner_kind="run",
                owner_id=run_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=start_payloads,
                phases=phases,
                authority_digest=recipe_digest,
                now=now,
                workload_intent_ordinal=workload_intent_ordinal,
                job_context=(
                    {"start_deadline": start_deadline}
                    if start_deadline is not None
                    else None
                ),
            )
        service._agent_jobs.notify_available()
        return service.get(job.id)

    def start(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._start_once(
                    plan,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    profile_application_id=profile_application_id,
                )
            except UnknownOutcomeError as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused

    def enqueue_one_shot_job_in_session(
        self,
        session: Session,
        *,
        artifact_job_id: str,
        run_id: str,
        node_id: str,
        payload: object,
        actor: str,
        request_id: str,
        authority_digest: str,
        now: datetime,
    ) -> Job:
        """Enqueue one fenced job without changing the service run lifecycle."""
        service = typing_cast("RecipeOperationService", self)
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is None or run.state != RunState.RUNNING:
            raise RecipeRequestInvalid("recipe run is not accepting jobs")
        node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run_id,
                RunNode.node_id == node_id,
                RunNode.state == RunState.RUNNING,
            )
        )
        if node is None:
            raise RecipeRequestInvalid("recipe job target is not running")
        return service._queue_in_session(
            session,
            kind=WireAgentOperation.RECIPE_JOB_RUN.value,
            owner_kind="artifact-job",
            owner_id=artifact_job_id,
            plan_digest=run.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=((node_id, payload),),
            authority_digest=authority_digest,
            now=now,
            workload_intent_ordinal=_run_start_intent(session, run_id),
        )

    def notify_agents(self) -> None:
        service = typing_cast("RecipeOperationService", self)
        service._agent_jobs.notify_available()
