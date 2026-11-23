"""Start for digest-bound recipe operations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentOperation as WireAgentOperation
from vonk_agent_protocol.gpu_clock import GpuClockRange
from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_agent_protocol.outcome import (
    InvalidRequestError,
    SecurityRefusalError,
    UnknownOutcomeError,
)
from vonk_agent_protocol.recipe_operations import RecipeStartPayload
from vonk_agent_protocol.state_machines import RunState
from vonk_agent_protocol.wire_model import canonical_message

from ..admission_locking import (
    AdmissionLockBusy,
    ConstraintReplay,
    acquire_admission_keys,
    admission_attempts,
    admission_wait_exhausted,
    node_admission_key,
)
from ..lifecycle.evidence import (
    Residue,
)
from ..models.execution import RecipeInstallation, RecipeRun, RunNode
from ..models.fleet import AgentNodeContact
from ..models.jobs import Job
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
from ..stored.json_values import read_row_column
from .errors import (
    RecipeRequestInvalid,
    RecipeRetryLater,
    retry_later_unless_deterministic,
)
from .interfaces import RecipeOperationView
from .observation_helpers import (
    _active_recipe_revision,
    _aware,
    run_takes_jobs,
    wait_for_installation_upkeep,
)
from .progress import (
    _canonical_distributed_readiness,
    _role_phases,
    _topology_order,
)
from .runtime_specs import recipe_engine, recipe_topology
from .start_payloads import (
    RecipeStartPayloadError,
    RecipeStartPlacement,
    build_recipe_start_payload,
    start_peer_addresses,
)

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
        gpu_clock: GpuClockRange | None = None,
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
                    tuple(node_admission_key(node.node_id) for node in plan.nodes),
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
            )
            if replay is not None:
                return replay
            presences = dict(
                session.execute(
                    select(
                        AgentNodeContact.node_id, AgentNodeContact.management_address
                    ).where(
                        AgentNodeContact.node_id.in_(
                            [node.node_id for node in plan.nodes]
                        ),
                        AgentNodeContact.management_address.is_not(None),
                    )
                ).all()
            )
            absent = sorted({node.node_id for node in plan.nodes} - set(presences))
            if absent:
                # A Spark reports its address when it next contacts the Controller.
                raise RecipeRetryLater(
                    "recipe node endpoint evidence is unavailable for "
                    + ", ".join(absent)
                )
            master = next((node for node in plan.nodes if node.endpoint_owner), None)
            if master is None:
                raise RecipeRetryLater("recipe run has no endpoint owner")
            world_size = len(plan.nodes)
            master_address = master.fabric_address if world_size > 1 else None
            master_port = master.rendezvous_port if world_size > 1 else None
            if world_size > 1 and (master_address is None or master_port is None):
                raise RecipeRetryLater("recipe direct-fabric rendezvous is unavailable")
            wait_for_installation_upkeep(session, plan.installation_id)
            try:
                run_id = service._run_admission.accept_run_in_session(
                    session,
                    plan,
                    actor=actor,
                    now=now,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except (SecurityRefusalError, InvalidRequestError, UnknownOutcomeError):
                raise
            except (RuntimeError, ValueError, TypeError, OSError) as error:
                raise retry_later_unless_deterministic(
                    "run admission", error
                ) from error
            run = session.get(RecipeRun, run_id)
            revision = _active_recipe_revision(session, plan.recipe_revision_id)
            installation = session.get(RecipeInstallation, plan.installation_id)
            assert run is not None
            assert revision is not None
            assert installation is not None
            loaded_plans = service._stored_compiled_plans(
                session,
                installation,
                [node.node_id for node in plan.nodes],
                now=now,
            )
            if isinstance(loaded_plans, Residue):
                raise RecipeRetryLater(
                    "compiled execution plan is unavailable for the installed recipe; "
                    "it is recorded and the next preparation installs afresh"
                )
            compiled_plans = loaded_plans
            document = read_row_column(revision, "document")
            if isinstance(document, Residue):
                raise RecipeRetryLater("recipe topology is invalid")
            start_order = _topology_order(document, "start_order")
            if start_order is None:
                raise RecipeRetryLater("recipe topology is invalid")
            topology = recipe_topology(document)
            distributed_readiness = _canonical_distributed_readiness(document)
            two_phase_start = (
                world_size > 1 and topology.distributed and distributed_readiness
            )
            start_deadline = (
                # Persist the accepted loading/JIT budget; neither lease renewal
                # nor retry extends a deadline.
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
            engine = recipe_engine(document)
            fabric_by_rank = {item.rank: item.fabric_address for item in plan.nodes}

            def start_payload(node: RunNodePlan) -> tuple[str, RecipeStartPayload]:
                endpoint_owner = node.endpoint_owner
                node_id = node.node_id
                try:
                    endpoint_address = (
                        presences[node_id] if endpoint_owner else node.fabric_address
                    )
                    if not isinstance(endpoint_address, str):
                        raise RecipeRetryLater(
                            f"recipe start endpoint address is unavailable for {node_id}"
                        )
                    peer_addresses = start_peer_addresses(
                        engine, world_size, fabric_by_rank
                    )
                    payload = build_recipe_start_payload(
                        run_id=run_id,
                        installation_id=plan.installation_id,
                        recipe_revision_id=plan.recipe_revision_id,
                        mapping_id=run.mapping_id,
                        run_generation=run.run_generation,
                        plan_digest=run.plan_digest,
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
                        peer_addresses=peer_addresses,
                        phase="rank-launch" if start_deadline is not None else None,
                        start_deadline=start_deadline,
                    )
                except RecipeStartPayloadError as error:
                    # The installed plan and the admitted placement are settled
                    # facts (endpoint evidence and every node's compiled plan
                    # were proven above), so every attempt builds the same
                    # payload: the start ends with the cause, never waits.
                    raise RecipeStartPayloadError(
                        f"recipe start payload for {node_id} cannot be built: {error}",
                        reason=InvalidRequestReason.UNSUPPORTED,
                    ) from error
                typed = RecipeStartPayload.model_validate_json(
                    canonical_message(payload)
                )
                if profile_application_id is not None or gpu_clock is not None:
                    from ..gpu.clock import gpu_clock_start_intent

                    try:
                        desired_clock = gpu_clock_start_intent(session, node_id)
                    except ValueError as error:
                        raise RecipeRequestInvalid(
                            str(error), reason=InvalidRequestReason.UNSUPPORTED
                        ) from error
                    if gpu_clock is not None and (
                        desired_clock is None or desired_clock.range != gpu_clock
                    ):
                        raise RecipeRequestInvalid(
                            "Reviewed GPU clock range differs from the selected profile; re-plan against the latest profile assignment",
                            reason=(
                                InvalidRequestReason.SUPERSEDED
                                if profile_application_id is not None
                                else InvalidRequestReason.CONFLICT
                            ),
                        )
                    typed = typed.model_copy(update={"gpu_clock": desired_clock})
                return node_id, typed

            start_payloads = tuple(
                (node_id, json.loads(canonical_message(payload)))
                for node_id, payload in (start_payload(node) for node in plan.nodes)
            )
            role_phases = _role_phases(start_order, start_payloads)
            if role_phases is None:
                # Starting blind (without the recipe's role order) could launch
                # a rank before what it depends on.
                raise RecipeRetryLater(
                    "operation topology order observation is unavailable"
                )
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
                plan_digest=run.plan_digest,
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
        gpu_clock: GpuClockRange | None = None,
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
                    gpu_clock=gpu_clock,
                )
            except ConstraintReplay as duplicate:
                return duplicate.replay(
                    lambda: service._idempotent(
                        request_id,
                        WireAgentOperation.RECIPE_START.value,
                        None,
                        installation_id=plan.installation_id,
                    )
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
        payloads: Sequence[tuple[str, object]],
        actor: str,
        request_id: str,
        authority_digest: str,
        now: datetime,
    ) -> Job:
        """Enqueue one job fenced by its run, never by its Spark's workload intent.

        ``payloads`` is one order per rank, every rank of the job at once: a
        JobRun only completes on exit, so a phased start order would deadlock
        the ranks that wait for each other.

        The job belongs to its run: it is admitted while the run and its node
        are RUNNING and no Stop of the run is underway, and the host grant
        binds its container to the run's generation. A load or Stop of another
        run on the same Spark never fences it; the run's own Stop cancels it.
        """
        service = typing_cast("RecipeOperationService", self)
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is None or run.state != RunState.RUNNING:
            raise RecipeRetryLater("recipe run observation is not accepting jobs")
        if not run_takes_jobs(session, run):
            # An accepted Stop of this run waits on its jobs' cancellation
            # with the run still RUNNING: a job admitted now would escape it.
            # Definite: a stopping run never takes jobs again.
            raise RecipeRequestInvalid(
                "recipe run is stopping", reason=InvalidRequestReason.SUPERSEDED
            )
        running = set(
            session.scalars(
                select(RunNode.node_id).where(
                    RunNode.run_id == run_id,
                    RunNode.node_id.in_([node_id for node_id, _ in payloads]),
                    RunNode.state == RunState.RUNNING,
                )
            )
        )
        if running != {node_id for node_id, _ in payloads}:
            raise RecipeRetryLater("recipe job target observation is not running")
        return service._queue_in_session(
            session,
            kind=WireAgentOperation.RECIPE_JOB_RUN.value,
            owner_kind="artifact-job",
            owner_id=artifact_job_id,
            plan_digest=run.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(payloads),
            # The parent records its exact children, the manifest the host-runtime
            # grant binds each job container's Start and Stop to.
            phases=(tuple(payloads),),
            authority_digest=authority_digest,
            now=now,
        )

    def notify_agents(self) -> None:
        service = typing_cast("RecipeOperationService", self)
        service._agent_jobs.notify_available()
