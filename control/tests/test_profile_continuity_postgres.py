"""Real SQL promises and issued cancellation receipts survive partial adoption."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    AgentResult,
    LifecycleState,
    WaitReason,
    WaitResumeCondition,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import (
    FleetProfileInput,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchQueueItem,
)
from vonk_control.fleet_profiles import (
    FleetProfileAdmissionBusy,
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _aware,
    _persisted_profile_progress,
)
from vonk_control.fleet_profiles.contracts import _FleetProfileSupersededIntentConflict
from vonk_control.fleet_profiles.persistence import _kept_fence_ordinal
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    Base,
    FleetProfileApplication,
    Job,
    ResourceReservation,
)
from vonk_control.persisted_waits import read_wait
from vonk_control.platform_ports import HOST_ENDPOINT_PORT
from vonk_control.run_switch_operations import RunSwitchOperationService

from .runtime_identity_support import claim_agent
from .test_agent_jobs import canonical_start_payload
from .test_fleet_profiles import (
    NOW,
    _assessment,
    _exact_preparation,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)
from .test_profile_adapter_continuity import _running_child


class _SQLCancellationAdapter(_SwitchAdapter):
    def request_superseded_workload_cancellation_in_session(
        self, session, targets, ordinal, now
    ):
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, targets, ordinal, now
        )


def _assessment_with_promises(_session, _assignment, expected_nodes, **_kwargs):
    assessment = _assessment(_exact_preparation(tuple(expected_nodes)))
    assessment.fit_current.nodes[0].ports_required = [HOST_ENDPOINT_PORT]
    assessment.fit_current.nodes[0].disk_required_bytes = 100
    return assessment


def test_postgres_partial_adoption_preserves_promises_and_waits_exact_issued_cleanup(
    postgres_engine, monkeypatch
):
    """Catches duplicate A promises, lost child identity and C dispatch before B ack."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    clock = [NOW]
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        session.flush()  # Materialize the node before its certificate FK.
        for index in (1, 2):
            session.add(
                AgentCertificate(
                    serial=f"continuity-{index}",
                    node_id=_node_id(index),
                    not_before=NOW - timedelta(seconds=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint=f"continuity-{index}",
                )
            )
    boundary = _SQLCancellationAdapter()
    profiles = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=boundary,
        assessment_provider=_assessment_with_promises,
    )
    choices = [
        {
            "recipe_selector": "vonk-forge/synthetic-tiny-build",
            "spark_ids": [_node_id(index)],
            "desired_state": "running",
            "assignment_name": f"lane-{index}",
        }
        for index in (1, 2)
    ]
    original_profile = profiles.create(
        FleetProfileInput.model_validate({"name": "A plus B", "assignments": choices}),
        actor="admin",
    )
    original = profiles.apply(
        original_profile.id, request_key=_uuid(18300), actor="admin"
    )
    assert profiles.tick()
    original_child = profiles.application(original.id).current_operation_id
    with sessions() as session:
        original_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == original.id
                )
            )
        )
        kept_claim_ids = {
            claim.id for claim in original_claims if claim.node_id == _node_id(1)
        }
        changed_claim_ids = {
            claim.id for claim in original_claims if claim.node_id == _node_id(2)
        }
        assert kept_claim_ids and changed_claim_ids
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    job_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid4()),
                kind="agent.operations",
                state="queued",
                actor="admin",
                authority_revision="a" * 64,
                targets=[_node_id(2)],
                payload_digest="b" * 64,
                payload={
                    "workload_intent_ordinal": original.progress.workload_intent_ordinal
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )
    # The issued effect is a launch: a newer load waits for a superseded launch
    # to stop, never for a Stop (cleanup runs alongside it).
    issued = jobs.enqueue(
        job_id,
        _node_id(2),
        "recipe.start",
        "a" * 64,
        canonical_start_payload(start_deadline=NOW + timedelta(seconds=30)),
    )
    claim = claim_agent(jobs, _node_id(2), "continuity-2")
    assert claim is not None
    replacement_choices = [choices[0], {**choices[1], "assignment_name": "lane-c"}]
    newer_profile = profiles.create(
        FleetProfileInput.model_validate(
            {"name": "A plus C", "assignments": replacement_choices}
        ),
        actor="admin",
    )
    clock[0] += timedelta(seconds=1)
    intermediate = profiles.apply(
        newer_profile.id, request_key=_uuid(18301), actor="admin"
    )
    # A second replacement supersedes the aggregate which borrowed A. The
    # durable link must stay flattened to A's original executor, never transfer
    # its child or promises to either later aggregate.
    final_profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "A plus D",
                "assignments": [
                    choices[0],
                    {**choices[1], "assignment_name": "lane-d"},
                ],
            }
        ),
        actor="admin",
    )
    final_review = profiles.preview(final_profile.id)
    assert [
        (effect.application_id, effect.node_ids)
        for effect in final_review.effects.adopted
    ] == [(original.id, [_node_id(1)])]
    clock[0] += timedelta(seconds=1)
    newer = profiles.apply(final_profile.id, request_key=_uuid(18304), actor="admin")
    assert profiles.application(intermediate.id).state == "superseded"
    assert profiles.application(original.id).current_operation_id == original_child
    restarted = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=boundary,
        assessment_provider=_assessment_with_promises,
    )
    with sessions.begin() as session:
        old = session.get(FleetProfileApplication, original.id)
        assert old is not None
        restarted._lifecycle.fail(
            old, "Lost child observation", clock[0], session=session
        )
        # The order backs off for its retry (its claims released) while its
        # child stays live.
        old.state = LifecycleState.BACKOFF.value
    assert restarted._resume_live_child(original.id) is not None
    with sessions() as session:
        old = session.get(FleetProfileApplication, original.id)
        assert old is not None
        assert restarted._adopted_application_scope(session, old) == (_node_id(1),)
        assert {
            claim.id
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == original.id,
                    ResourceReservation.node_id == _node_id(1),
                    ResourceReservation.state.in_(("active", "promised")),
                )
            )
        } == kept_claim_ids
        for identity in changed_claim_ids:
            changed_claim = session.get(ResourceReservation, identity)
            assert changed_claim is not None
            assert changed_claim.state == "released"
        port_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.kind == "port",
                    ResourceReservation.state == "promised",
                )
            )
        )
        assert {(value.node_id, value.owner_id) for value in port_claims} == {
            (_node_id(1), original.id),
            (_node_id(2), newer.id),
        }
        newer_ordinal = newer.progress.workload_intent_ordinal
        assert newer_ordinal is not None
        pending = AgentJobService.assess_superseded_agent_effects_in_session(
            session, (_node_id(2),), newer_ordinal, clock[0]
        )
        assert [effect.operation_id for effect in pending] == [issued.id]
    # Resume the replacement adapter's persisted queue with the real issued
    # effect in SQL. Only physical child dispatch is intercepted here; cleanup
    # observation, fencing, attempts and late receipts are the production path.
    adapter = RunSwitchFleetProfileAdapter(
        sessions, RunSwitchOperationService(sessions, clock=lambda: clock[0])
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, newer.id)
        assert row is not None
        intended = _persisted_profile_progress(row).intended_profile
        assert intended is not None
        assignment = next(
            value
            for value in intended.assignments
            if value.nodes[0].node_id == _node_id(2)
        )
        adapter._write_state(
            session,
            row,
            FleetProfileSwitchAdapterState(
                child_id=newer.id,
                scope_node_ids=[_node_id(2)],
                assignment_ids=[assignment.id],
                assignments=[assignment],
                queue=[FleetProfileSwitchQueueItem(kind="run", id=assignment.id)],
                actor="admin",
                request_id=_uuid(18302),
                state=LifecycleState.QUEUED,
            ),
        )
    dispatched = []

    def dispatch(*args):
        dispatched.append(args[1].id)
        return _running_child(_uuid(18303), _node_id(2))

    monkeypatch.setattr(adapter, "_start_child", dispatch)
    adapter.advance(newer.id)
    assert not dispatched
    with sessions() as session:
        row = session.get(FleetProfileApplication, newer.id)
        assert row is not None
        stored = _persisted_profile_progress(row).switch_adapter
        assert stored is not None
        assert stored.pending_operation_ids == [issued.id]
        wait = read_wait(row)
        assert wait is not None and wait.reason is WaitReason.STOP_UNCONFIRMED
        assert wait.resume_condition is WaitResumeCondition.EFFECT_OBSERVED
        assert wait.dependency_deadline == stored.observation_deadline_at
        assert [b.operation.id for b in wait.blockers if b.operation] == [issued.id]
    # A late exact cancellation receipt closes the original attempt. Advancing
    # the normal persisted backoff then releases C without another profile load.
    clock[0] += timedelta(seconds=700)
    assert jobs.record_late_result(
        AgentResult.model_validate(
            {
                "fence": claim.fence,
                "state": "cancelled",
                "result": {
                    "error_code": "operation_cancelled",
                    "reason": "exact stop completed",
                },
            }
        )
    )
    adapter.advance(newer.id)
    assert dispatched == [assignment.id]
    assert restarted.application(original.id).current_operation_id == original_child
    restarted.cancel(
        newer.id,
        profile_number=final_profile.number,
        request_key=_uuid(18305),
        actor="admin",
    )
    with sessions.begin() as session:
        old = session.get(FleetProfileApplication, original.id)
        assert old is not None
        restarted._lifecycle.fail(
            old, "Lost child observation", clock[0], session=session
        )
    assert restarted._resume_live_child(original.id) is None
    fresh = restarted.apply(final_profile.id, request_key=_uuid(18306), actor="admin")
    assert fresh.id not in {original.id, newer.id}
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}


def test_postgres_new_profile_never_adopts_half_of_a_dual_spark_effect(postgres_engine):
    """Catches preserving one rank of a replaced gang as independent work."""
    from .test_fleet_profiles import _seed_dual_solo_without_runtime_state

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed_dual_solo_without_runtime_state(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(3),
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    boundary = _SQLCancellationAdapter()
    profiles = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=boundary,
        assessment_provider=_assessment_with_promises,
    )
    solo = {
        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
        "spark_ids": [_node_id(3)],
        "desired_state": "running",
        "assignment_name": "independent",
    }
    dual = {
        "recipe_selector": "vonk-forge/synthetic-tiny-build",
        "spark_ids": [_node_id(1), _node_id(2)],
        "desired_state": "running",
        "assignment_name": "gang",
    }
    original_profile = profiles.create(
        FleetProfileInput.model_validate(
            {"name": "Gang and solo", "assignments": [dual, solo]}
        ),
        actor="admin",
    )
    original = profiles.apply(
        original_profile.id, request_key=_uuid(18400), actor="admin"
    )
    newer_profile = profiles.create(
        FleetProfileInput.model_validate(
            {"name": "Solo continues", "assignments": [solo]}
        ),
        actor="admin",
    )
    preview = profiles.preview(newer_profile.id)
    assert [
        (effect.application_id, effect.node_ids) for effect in preview.effects.adopted
    ] == [(original.id, [_node_id(3)])]
    assert not any(
        node_id in effect.node_ids
        for effect in preview.effects.adopted
        for node_id in (_node_id(1), _node_id(2))
    )


@dataclass
class _WaitingContinuation:
    """A load waiting for admission that continues lane 1 of an older load
    (on Spark 1) and replaces its lane 2 (on Spark 2) with lane C."""

    sessions: sessionmaker
    profiles: FleetProfileService
    clock: list[datetime]
    lanes: list[dict]
    original_id: str
    original_child: str
    waiting_id: str


def _waiting_continuation(postgres_engine, request_key: int) -> _WaitingContinuation:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    clock = [NOW]
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        session.flush()  # Materialize the node before its certificate FK.
        for index in (1, 2):
            session.add(
                AgentCertificate(
                    serial=f"waiting-continuity-{index}",
                    node_id=_node_id(index),
                    not_before=NOW - timedelta(seconds=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint=f"waiting-continuity-{index}",
                )
            )
    profiles = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=_SQLCancellationAdapter(),
        assessment_provider=_assessment_with_promises,
    )
    lanes = [
        {
            "recipe_selector": "vonk-forge/synthetic-tiny-build",
            "spark_ids": [_node_id(index)],
            "desired_state": "running",
            "assignment_name": f"lane-{index}",
        }
        for index in (1, 2)
    ]
    original_profile = profiles.create(
        FleetProfileInput.model_validate({"name": "A plus B", "assignments": lanes}),
        actor="admin",
    )
    original = profiles.apply(
        original_profile.id, request_key=_uuid(request_key), actor="admin"
    )
    assert profiles.tick()
    original_child = profiles.application(original.id).current_operation_id
    assert original_child is not None
    newer_profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "A plus C",
                "assignments": [lanes[0], {**lanes[1], "assignment_name": "lane-c"}],
            }
        ),
        actor="admin",
    )
    review = profiles.preview(newer_profile.id)
    assert [
        (effect.application_id, effect.node_ids) for effect in review.effects.adopted
    ] == [(original.id, [_node_id(1)])]
    clock[0] += timedelta(seconds=1)
    # Admission was busy when the load was accepted: it waits for its retry.
    waiting = profiles._create_pending_application(
        review,
        request_key=_uuid(request_key + 1),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    profiles._defer_pending_application(
        waiting.id, "initial contention", retry_delay=timedelta(0)
    )
    return _WaitingContinuation(
        sessions, profiles, clock, lanes, original.id, original_child, waiting.id
    )


def _progress(case: _WaitingContinuation, application_id: str):
    with case.sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        return row, _persisted_profile_progress(row)


def _retry_busy_admission(case: _WaitingContinuation, monkeypatch) -> None:
    """One admission retry fences the load's Sparks, then finds admission busy."""
    admit = case.profiles._queue_application
    refused: list[bool] = []

    def busy_once(*args, **kwargs):
        if not refused:
            refused.append(True)
            raise FleetProfileAdmissionBusy("another admission holds the lock")
        return admit(*args, **kwargs)

    monkeypatch.setattr(case.profiles, "_queue_application", busy_once)
    assert case.profiles._observe_pending_admissions(case.clock[0])
    row, progress = _progress(case, case.waiting_id)
    assert progress.admission_pending, row.status_reason
    assert row.next_action_at is not None
    case.clock[0] = max(case.clock[0], _aware(row.next_action_at))


def test_postgres_waiting_load_keeps_the_lane_it_continues_and_is_admitted(
    postgres_engine,
):
    """A load waiting for admission continues one lane of an older load and
    changes the other. Fencing its changed lane must not retire the older load
    that owns the continued lane: that made its own reviewed plan stale, and the
    re-planned (wider) fence then read its own older ordinal as a newer intent
    and superseded the load. It is admitted without a person, first time."""
    case = _waiting_continuation(postgres_engine, 18500)
    profiles = case.profiles

    assert profiles._observe_pending_admissions(case.clock[0])

    row, progress = _progress(case, case.waiting_id)
    assert row.state in {"queued", "running"}, row.status_reason
    assert not progress.admission_pending, progress.blockers
    assert progress.workload_intent_ordinal is not None
    older, _ = _progress(case, case.original_id)
    # The continued lane keeps its owner and its child.
    assert older.state != "superseded", older.status_reason
    assert older.current_operation_id == case.original_child
    # Its own worker dispatches it: nobody reviews it again.
    assert profiles.tick()
    admitted = profiles.application(case.waiting_id)
    assert admitted.state == "running", admitted.status_reason
    assert admitted.current_operation_id is not None
    assert profiles.application(case.original_id).current_operation_id == (
        case.original_child
    )


def test_postgres_waiting_load_is_admitted_when_the_lane_it_continued_ends(
    postgres_engine, monkeypatch
):
    """The older owner of the continued lane ends on its own between two
    admission retries, so the re-planned load starts that lane itself and its
    fence widens to that Spark. The Spark it never fenced still carries the
    older load's ordinal: that is a Spark joining the fence, not a newer intent
    taking it. The load is admitted and starts its child, never superseded."""
    case = _waiting_continuation(postgres_engine, 18510)
    profiles = case.profiles
    _retry_busy_admission(case, monkeypatch)
    _, fenced = _progress(case, case.waiting_id)
    assert fenced.workload_intent_ordinal is not None
    with case.sessions.begin() as session:
        older = session.get(FleetProfileApplication, case.original_id)
        assert older is not None
        profiles._lifecycle.fail(
            older, "Lost child observation", case.clock[0], session=session
        )

    # The next retry finds the continued lane gone and plans it again (now
    # starting that lane itself), retrying at once: a conflict re-plans.
    assert profiles._observe_pending_admissions(case.clock[0])
    row, replanned = _progress(case, case.waiting_id)
    assert replanned.admission_pending
    assert row.next_action_at is not None
    assert _aware(row.next_action_at) <= case.clock[0]

    assert profiles._observe_pending_admissions(case.clock[0])

    row, progress = _progress(case, case.waiting_id)
    assert row.state in {"queued", "running"}, row.status_reason
    assert not progress.admission_pending, progress.blockers
    assert not progress.blockers
    assert progress.supersede_code is None
    with case.sessions() as session:
        assert {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode))
        } == {
            _node_id(1): progress.workload_intent_ordinal,
            _node_id(2): progress.workload_intent_ordinal,
        }
    assert profiles.tick()
    admitted = profiles.application(case.waiting_id)
    assert admitted.state == "running", admitted.status_reason
    assert admitted.current_operation_id is not None


def test_postgres_newer_intent_on_a_fenced_spark_supersedes_the_waiting_load(
    postgres_engine, monkeypatch
):
    """The inverse: a newer load accepted for a Spark the waiting load already
    fenced is the newest request and steers; the waiting load ends superseded
    by it and the newer load is admitted."""
    case = _waiting_continuation(postgres_engine, 18520)
    profiles = case.profiles
    _retry_busy_admission(case, monkeypatch)
    newest_profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "A plus D",
                "assignments": [
                    case.lanes[0],
                    {**case.lanes[1], "assignment_name": "lane-d"},
                ],
            }
        ),
        actor="admin",
    )
    case.clock[0] += timedelta(seconds=1)
    newest = profiles.apply(newest_profile.id, request_key=_uuid(18522), actor="admin")
    assert newest.state in {"queued", "running"}, newest.status_reason

    profiles._observe_pending_admissions(case.clock[0])

    row, progress = _progress(case, case.waiting_id)
    assert row.state == "superseded", row.status_reason
    assert progress.supersede_code == "superseded-by-intent"
    assert progress.superseded_by == newest.id
    assert profiles.application(newest.id).state in {"queued", "running"}


def _sparks(**ordinals: int) -> list[AgentNode]:
    return [
        AgentNode(node_id=node_id, workload_intent_ordinal=ordinal)
        for node_id, ordinal in ordinals.items()
    ]


@pytest.mark.parametrize(
    ("ordinals", "fenced", "kept"),
    [
        # Every Spark it fenced still carries its ordinal: it keeps it.
        ({"a": 4, "b": 4}, ["a", "b"], 4),
        # A Spark joined the fence, older owner numbered higher or lower:
        # it fences anew.
        ({"a": 4, "b": 9}, ["a"], None),
        ({"a": 4, "b": 1}, ["a"], None),
        # The fence narrowed: what is left is still its own.
        ({"a": 4}, ["a", "b"], 4),
        # Recorded before the fence was: it fences anew.
        ({"a": 4}, [], None),
    ],
)
def test_a_spark_joining_a_waiting_fence_is_no_newer_intent(ordinals, fenced, kept):
    assert _kept_fence_ordinal(_sparks(**ordinals), 4, fenced) == kept


def test_a_spark_the_waiting_load_fenced_and_lost_supersedes_it():
    with pytest.raises(_FleetProfileSupersededIntentConflict):
        _kept_fence_ordinal(_sparks(a=5, b=9), 4, ["a"])
