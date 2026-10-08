"""Real faulty warn-mode producer, restart, exact repair and native continuation."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    LifecycleState,
    OperationProgress,
    canonical_message,
)
from vonk_control import run_switch_operations as owner
from vonk_control.agent_jobs import AgentJobService
from vonk_control.job_documents import RunSwitchRunIntent
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    AgentOperation,
    Base,
    Job,
    ResourceReservation,
    RunSwitchJournalRepair,
    RunSwitchJournalRepairPending,
)
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_switch_journal_contract import (
    JournalRepairDisposition,
    RunSwitchJournalRepairEvidence,
)
from vonk_control.run_switch_journal_repair import (
    journal_document,
    try_repair_zero_transfer_journal,
)
from vonk_control.stored_json import write_guard_mode

from .runtime_identity_support import claim_agent
from .test_fleet_profile_api import _client, _headers
from .test_profile_child_progress_projection import (
    _drive_install,
    _follow_saved_due,
    _measured_profile_service,
)
from .test_profile_installed_execution import _apply, _installed_profile
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)
from .test_recipe_operations import setup_services

pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = pytest.mark.needs_cli_dependencies


@pytest.fixture
def faulty_install(tmp_path, postgres_engine, monkeypatch):
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    clock = [lifecycle._clock()]
    lifecycle._clock = lambda: clock[0]
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    jobs.set_result_consumer(lifecycle.consume_agent_result)
    lifecycle._agent_jobs = jobs
    profiles, planner = _measured_profile_service(sessions, lifecycle)
    application = _apply(profiles, _installed_profile(profiles, sessions, nodes))
    (install_id,) = _drive_install(
        sessions, profiles, planner, application.id, clock, 1
    )
    claim = claim_agent(jobs, nodes[0], "serial-0", 60)
    assert claim is not None and claim.operation == "recipe.install"
    for completed in (32, 48):
        jobs.heartbeat(
            claim,
            OperationProgress(
                phase="copying",
                completed_bytes=completed,
                total_bytes=64,
                total_bytes_known=True,
            ),
            60,
        )
        clock[0] += timedelta(seconds=2)
    with sessions() as session:
        switch = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert switch is not None
        switch_id = switch.id
        identity = (
            switch.id,
            switch.request_id,
            switch.payload_digest,
            copy.deepcopy(switch.payload),
        )
        accepted_plan = owner._stored_job_plan(switch)
        assert accepted_plan is not None
        assert owner._planned_transfer_parts(accepted_plan) == (
            0,
            0,
            0,
        )
    original_merge = owner._merge_progress_evidence

    def historical_merge(progress, plan, phase, evidence, now=None):
        # Isolated wrong-algorithm producer: the historical bug counted native
        # phase bytes in distribution. Input is the REAL native child projection,
        # never fabricated progress or a direct SQL journal mutation.
        original_merge(progress, plan, phase, evidence, now)
        if phase.kind != "transfer" and evidence.completed_bytes is not None:
            progress.completed_bytes = max(
                progress.completed_bytes, evidence.completed_bytes
            )

    with monkeypatch.context() as fault:
        fault.setattr(owner, "_merge_progress_evidence", historical_merge)
        with write_guard_mode(strict=False):
            assert planner.tick()
    assert owner._merge_progress_evidence is original_merge
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.result is not None and row.state == "running"
        raw = copy.deepcopy(row.result)
        assert raw["completed_bytes"] == 48 and raw["total_bytes"] == 0
        assert isinstance(owner._stored_result(raw), Residue)
        claims = [
            (item.id, item.state, item.amount_bytes)
            for item in session.scalars(select(ResourceReservation))
        ]
        native_ids = set(session.scalars(select(AgentOperation.id)))
    return (
        sessions,
        lifecycle,
        profiles,
        planner,
        jobs,
        clock,
        nodes,
        application,
        switch_id,
        install_id,
        claim,
        identity,
        raw,
        claims,
        native_ids,
    )


def test_warn_mode_fault_repairs_same_child_then_native_receipt_reaches_installed_cli(
    faulty_install, installed_vonkctl, tmp_path
):
    (
        sessions,
        lifecycle,
        profiles,
        _planner,
        _jobs,
        clock,
        nodes,
        application,
        switch_id,
        install_id,
        claim,
        identity,
        raw,
        claims,
        native_ids,
    ) = faulty_install
    # Recreate ALL owners; no draft/apply/resubmit, direct SQL repair or reset.
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    restarted = RecipeOperationService(
        sessions,
        install_admission=lifecycle._install_admission,
        run_admission=lifecycle._run_admission,
        agent_jobs=jobs,
        clock=lambda: clock[0],
    )
    jobs.set_result_consumer(restarted.consume_agent_result)
    profiles, planner = _measured_profile_service(sessions, restarted)
    assert planner.tick()
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.result is not None
        assert (row.id, row.request_id, row.payload_digest, row.payload) == identity
        repaired = owner._stored_result(row.result)
        assert repaired is not None and not isinstance(repaired, Residue)
        assert repaired.completed_bytes == 0 and repaired.total_bytes == 0
        assert (
            repaired.operation is not None and repaired.operation.completed_bytes == 48
        )
        assert repaired.child_operation_id == install_id
        expected = dict(raw)
        expected["completed_bytes"] = 0
        assert row.result == expected
        [audit] = list(session.scalars(select(RunSwitchJournalRepair)))
        evidence = RunSwitchJournalRepairEvidence.model_validate_json(
            canonical_message(audit.evidence), strict=True
        )
        assert evidence.original_document == journal_document(raw)
        assert (
            evidence.original_digest
            == hashlib.sha256(journal_document(raw).encode()).hexdigest()
        )
        assert (
            evidence.operation_id == switch_id
            and evidence.native_samples[0].fence == claim.fence
        )
        assert [
            (item.id, item.state, item.amount_bytes)
            for item in session.scalars(select(ResourceReservation))
        ] == claims
        assert set(session.scalars(select(AgentOperation.id))) == native_ids
    # Idempotent reentry leaves original evidence and current child untouched.
    assert (
        try_repair_zero_transfer_journal(sessions, switch_id, clock[0])
        == JournalRepairDisposition.NOT_APPLICABLE
    )
    clock[0] += timedelta(seconds=2)
    jobs.heartbeat(
        claim,
        OperationProgress(
            phase="copying", completed_bytes=56, total_bytes=64, total_bytes_known=True
        ),
        60,
    )
    assert planner.tick()
    switch = planner.get(switch_id)
    assert switch.result is not None and switch.result.operation is not None
    assert (
        switch.result.operation.completed_bytes == 56
        and switch.result.completed_bytes == 0
    )
    # Observe the repaired sample through the real API and installed strict CLI
    # while the ORIGINAL native child is still pending, not just after terminal.
    _follow_saved_due(sessions, profiles, application.id, clock)
    profiles.tick()
    api, tokens = _client(sessions, profiles=profiles)
    headers = _headers(tokens, "administrator")
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer):
        measured_result = subprocess.run(
            [
                str(installed_vonkctl),
                "profile",
                "progress",
                "--application",
                application.id,
                "--json",
            ],
            cwd=tmp_path,
            env=_process_environment(tmp_path, url, certificate, headers),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert measured_result.returncode == 0, (
            measured_result.stdout + measured_result.stderr
        )
        measured_document = json.loads(measured_result.stdout)
    assert measured_document["id"] == application.id
    measured_effects = [
        item
        for item in measured_document["progress"]["effects"]
        if item["kind"] == "install"
    ]
    assert len(measured_effects) == 1
    measured = measured_effects[0]["progress"]["operation"]
    assert measured["completed_bytes"] == 56
    assert {member["member_id"] for member in measured["members"]} == set(nodes)
    assert measured_effects[0]["operation_id"] == switch_id
    with sessions() as session:
        still_pending = session.get(Job, install_id)
        assert still_pending is not None and still_pending.state == "running"
    # Complete through actual fenced native result consumer, including the
    # next role's real claim. A terminal parent alone cannot free any scope.
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": claim.fence,
                    "state": "succeeded",
                    "result": {"installed_bytes": 64},
                }
            ),
            strict=True,
        )
    )
    second = claim_agent(jobs, nodes[1], "serial-1", 60)
    assert second is not None and second.operation == "recipe.install"
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": second.fence,
                    "state": "succeeded",
                    "result": {"installed_bytes": 64},
                }
            ),
            strict=True,
        )
    )
    assert restarted.get(install_id).state == "succeeded"
    planner.tick()
    profiles.tick()
    api, tokens = _client(sessions, profiles=profiles)
    headers = _headers(tokens, "administrator")
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer):
        result = subprocess.run(
            [
                str(installed_vonkctl),
                "profile",
                "progress",
                "--application",
                application.id,
                "--json",
            ],
            cwd=tmp_path,
            env=_process_environment(tmp_path, url, certificate, headers),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        document = json.loads(result.stdout)
    assert document["id"] == application.id
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.request_id == identity[1]
        assert (
            len(list(session.scalars(select(Job).where(Job.kind == "recipe.install"))))
            == 1
        )
        assert len(list(session.scalars(select(RunSwitchJournalRepair)))) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "changed-sample",
        "second-attempt",
        "missing-attempt",
        "wrong-child-request",
        "newer-ordinal",
    ],
)
def test_unproven_owner_preserves_raw_journal_during_bounded_observation(
    faulty_install, fault
):
    (
        sessions,
        _lifecycle,
        _profiles,
        _planner,
        jobs,
        clock,
        _nodes,
        _application,
        switch_id,
        install_id,
        claim,
        _identity,
        raw,
        claims,
        _native_ids,
    ) = faulty_install
    if fault == "changed-sample":
        clock[0] += timedelta(seconds=2)
        jobs.heartbeat(
            claim,
            OperationProgress(
                phase="copying",
                completed_bytes=56,
                total_bytes=64,
                total_bytes_known=True,
            ),
            60,
        )
    elif fault == "second-attempt":
        from vonk_control.models import AgentOperationAttempt

        with sessions() as session:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == claim.fence
                )
            )
            assert attempt is not None
            clock[0] = attempt.lease_deadline + timedelta(microseconds=1)
        jobs.reconcile_orders()
        with sessions() as session:
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == install_id,
                    AgentOperation.node_id == _nodes[0],
                )
            )
            assert operation is not None
            if (
                operation.next_action_at is not None
                and operation.next_action_at > clock[0]
            ):
                clock[0] = operation.next_action_at
        jobs.reconcile_orders()
        retry = claim_agent(jobs, _nodes[0], "serial-0", 60)
        assert (
            retry is not None
            and retry.operation == "recipe.install"
            and retry.fence != claim.fence
        )
        jobs.heartbeat(
            retry,
            OperationProgress(
                phase="copying",
                completed_bytes=48,
                total_bytes=64,
                total_bytes_known=True,
            ),
            60,
        )
    else:
        # Explicit corruption negatives, under the seed-only guard. The initial
        # cross-domain fault above is always produced by the genuine old merger.
        from sqlalchemy import delete
        from vonk_control.models import AgentNode, AgentOperationAttempt

        with sessions.begin() as session, write_guard_mode(strict=False):
            child = session.get(Job, install_id)
            row = session.get(Job, switch_id)
            assert child is not None and row is not None
            if fault == "missing-attempt":
                session.execute(
                    delete(AgentOperationAttempt).where(
                        AgentOperationAttempt.fence == claim.fence
                    )
                )
            elif fault == "wrong-child-request":
                import uuid

                child.request_id = str(uuid.uuid4())
            elif fault == "newer-ordinal":
                node = session.get(AgentNode, row.targets[0])
                assert node is not None
                node.workload_intent_ordinal += 1
    result = try_repair_zero_transfer_journal(sessions, switch_id, clock[0])
    assert result == JournalRepairDisposition.DEFERRED
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.result == raw
        assert [
            (item.id, item.state, item.amount_bytes)
            for item in session.scalars(select(ResourceReservation))
        ] == claims
        assert not list(session.scalars(select(RunSwitchJournalRepair)))


def test_startup_adds_evidence_storage_without_replacing_issued_checkpoint(
    faulty_install, postgres_engine
):
    from sqlalchemy import inspect
    from vonk_control.db import initialize_database

    (
        sessions,
        _lifecycle,
        _profiles,
        _planner,
        _jobs,
        clock,
        _nodes,
        _application,
        switch_id,
        install_id,
        _claim,
        identity,
        raw,
        claims,
        native_ids,
    ) = faulty_install
    # Simulate only the missing NEW empty table in the prior schema, not a DB
    # reset or removal of any existing accepted work/evidence.
    Base.metadata.tables["run_switch_journal_repairs"].drop(postgres_engine)
    before = set(inspect(postgres_engine).get_table_names())
    assert "run_switch_journal_repairs" not in before
    initialize_database(postgres_engine.url.render_as_string(hide_password=False))
    assert set(inspect(postgres_engine).get_table_names()) == before | {
        "run_switch_journal_repairs",
        "alembic_version",
    }
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert (
            row is not None
            and (row.id, row.request_id, row.payload_digest, row.payload) == identity
        )
        assert row.result == raw and row.state == "running"
        assert set(session.scalars(select(AgentOperation.id))) == native_ids
        assert [
            (item.id, item.state, item.amount_bytes)
            for item in session.scalars(select(ResourceReservation))
        ] == claims
    with write_guard_mode(strict=True):
        assert (
            try_repair_zero_transfer_journal(sessions, switch_id, clock[0])
            == JournalRepairDisposition.REPAIRED
        )
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None
        repaired = owner._stored_result(row.result)
        assert repaired is not None and not isinstance(repaired, Residue)
        assert repaired.child_operation_id == install_id
        assert len(list(session.scalars(select(RunSwitchJournalRepair)))) == 1


@pytest.mark.parametrize("held_kind", ["parent", "node", "native"])
def test_held_required_row_defers_without_clock_reset_and_disjoint_work_advances(
    faulty_install, held_kind
):
    from concurrent.futures import ThreadPoolExecutor

    from vonk_control.models import AgentNode
    from vonk_control.terminal_history_collection import TerminalHistoryCollector

    from .test_terminal_history_collection import _job

    (
        sessions,
        _lifecycle,
        _profiles,
        planner,
        _jobs,
        clock,
        nodes,
        _application,
        switch_id,
        install_id,
        _claim,
        identity,
        raw,
        claims,
        native_ids,
    ) = faulty_install
    original_clock = clock[0]
    due = raw.get("observation_due_at")
    deadline = raw.get("observation_deadline_at")
    sibling = _job(clock[0] - timedelta(days=2))
    with sessions.begin() as session:
        session.add(sibling)
    holder = sessions()
    holder.begin()
    if held_kind == "parent":
        statement = select(Job).where(Job.id == switch_id)
    elif held_kind == "node":
        statement = select(AgentNode).where(AgentNode.node_id == nodes[0])
    else:
        statement = select(AgentOperation).where(
            AgentOperation.parent_job_id == install_id,
            AgentOperation.node_id == nodes[0],
        )
    assert holder.scalar(statement.with_for_update()) is not None
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        # Includes the REAL worker's post-advance wait observer, not just a
        # direct helper call that could conceal a second blocking row lock.
        assert executor.submit(planner.tick).result(timeout=1) is False
        assert clock[0] == original_clock
        assert TerminalHistoryCollector(sessions, clock=lambda: clock[0]).tick()
        with sessions() as session:
            row = session.get(Job, switch_id)
            assert row is not None and row.result == raw
            assert (row.id, row.request_id, row.payload_digest, row.payload) == identity
            assert row.result.get("observation_due_at") == due
            assert row.result.get("observation_deadline_at") == deadline
            assert session.get(Job, sibling.id) is None
            assert set(session.scalars(select(AgentOperation.id))) == native_ids
            assert [
                (item.id, item.state, item.amount_bytes)
                for item in session.scalars(select(ResourceReservation))
            ] == claims
            assert not list(session.scalars(select(RunSwitchJournalRepair)))
    finally:
        holder.rollback()
        holder.close()
        executor.shutdown(wait=True)
    with sessions() as session:
        pending = session.get(RunSwitchJournalRepairPending, switch_id)
        if pending is not None:
            from vonk_control.run_switch_journal_contract import (
                RunSwitchJournalRepairPendingState,
            )

            state = RunSwitchJournalRepairPendingState.model_validate_json(
                canonical_message(pending.progress), strict=True
            )
            clock[0] = state.next_attempt_at
    assert planner.tick()
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.request_id == identity[1]
        repaired = owner._stored_result(row.result)
        assert repaired is not None and not isinstance(repaired, Residue)
        assert repaired.child_operation_id == install_id
        assert row.result["observation_due_at"] == due
        assert row.result.get("observation_deadline_at") == deadline
        assert len(list(session.scalars(select(RunSwitchJournalRepair)))) == 1


@pytest.mark.parametrize("repair_race", [False, True])
def test_fault_cancel_preserves_issued_child_until_late_exact_fenced_receipts(
    faulty_install,
    monkeypatch,
    repair_race,
):
    from uuid import uuid4

    from vonk_control.run_switch_journal_contract import JournalRepairPurpose

    (
        sessions,
        lifecycle,
        _profiles,
        planner,
        jobs,
        _clock,
        nodes,
        _application,
        switch_id,
        install_id,
        claim,
        identity,
        raw,
        claims,
        native_ids,
    ) = faulty_install
    if not repair_race:
        # A changed genuine sample prevents measurement repair. It must never
        # prevent cancellation of this exactly bound issued child.
        jobs.heartbeat(
            claim,
            OperationProgress(
                phase="copying",
                completed_bytes=56,
                total_bytes=64,
                total_bytes_known=True,
            ),
            60,
        )
    else:
        from vonk_control import run_switch_journal_repair as repair_owner

        original_record = repair_owner.record_repair_cancellation

        def repair_before_record(*args):
            assert (
                try_repair_zero_transfer_journal(sessions, switch_id, _clock[0])
                == JournalRepairDisposition.REPAIRED
            )
            original_record(*args)

        monkeypatch.setattr(
            repair_owner, "record_repair_cancellation", repair_before_record
        )
    cancellation = planner.cancel(
        switch_id,
        actor="admin",
        request_key=str(uuid4()),
        reason="Stop observing this accepted preparation",
    )
    assert cancellation.state != "cancelled"
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.request_id == identity[1]
        progress = owner._stored_result(row.result)
        assert progress is not None and not isinstance(progress, Residue)
        assert (
            progress.child_operation_id == install_id
            and progress.cancellation is not None
        )
        [audit] = list(session.scalars(select(RunSwitchJournalRepair)))
        evidence = RunSwitchJournalRepairEvidence.model_validate_json(
            canonical_message(audit.evidence), strict=True
        )
        assert evidence.purpose == (
            JournalRepairPurpose.MEASUREMENT
            if repair_race
            else JournalRepairPurpose.CANCELLATION
        )
        assert evidence.original_document == journal_document(raw)
        assert [
            (item.id, item.state, item.amount_bytes)
            for item in session.scalars(select(ResourceReservation))
        ] == claims
        assert set(session.scalars(select(AgentOperation.id))) == native_ids
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": claim.fence,
                    "state": "succeeded",
                    "result": {"installed_bytes": 64},
                }
            ),
            strict=True,
        )
    )
    second = claim_agent(jobs, nodes[1], "serial-1", 60)
    assert second is not None and second.operation == "recipe.install"
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": second.fence,
                    "state": "succeeded",
                    "result": {"installed_bytes": 64},
                }
            ),
            strict=True,
        )
    )
    assert lifecycle.get(install_id).state == "succeeded"
    assert planner.tick()
    closed = planner.get(switch_id)
    assert closed.state == "cancelled" and closed.request_key == identity[1]
    with sessions() as session:
        assert (
            len(list(session.scalars(select(Job).where(Job.kind == "recipe.install"))))
            == 1
        )
        assert len(list(session.scalars(select(RunSwitchJournalRepair)))) == 1
    # The settled preparation cannot retain a same-Spark admission hold.
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None
        parent = owner._run_switch_payload(row)
        assert parent is not None
        assert isinstance(parent.intent, RunSwitchRunIntent)
        request = parent.intent.request
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        closed,
        end=lambda _receipt: planner.get(switch_id),
        fresh=lambda _world: planner.apply(
            request.model_copy(
                update={"request_key": str(uuid4()), "plan_digest": None}
            ),
            actor="admin",
        ),
    )


def test_native_lease_expiry_reconciles_same_owner_when_sample_repair_is_deferred(
    faulty_install,
):
    from vonk_control.models import AgentOperationAttempt
    from vonk_control.run_switch_journal_contract import JournalRepairPurpose

    (
        sessions,
        _lifecycle,
        _profiles,
        planner,
        jobs,
        clock,
        _nodes,
        _application,
        switch_id,
        install_id,
        claim,
        identity,
        raw,
        _claims,
        native_ids,
    ) = faulty_install
    jobs.heartbeat(
        claim,
        OperationProgress(
            phase="copying", completed_bytes=56, total_bytes=64, total_bytes_known=True
        ),
        60,
    )
    assert (
        try_repair_zero_transfer_journal(sessions, switch_id, clock[0])
        == JournalRepairDisposition.DEFERRED
    )
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        deadline = attempt.lease_deadline
    clock[0] = deadline + timedelta(microseconds=1)
    # Follow only the actual fixed owner deadline; no SQL deadline surgery.
    assert planner.tick()
    jobs.reconcile_orders()
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.request_id == identity[1]
        progress = owner._stored_result(row.result)
        assert progress is not None and not isinstance(progress, Residue)
        assert progress.child_operation_id == install_id
        [audit] = list(session.scalars(select(RunSwitchJournalRepair)))
        evidence = RunSwitchJournalRepairEvidence.model_validate_json(
            canonical_message(audit.evidence), strict=True
        )
        assert evidence.purpose == JournalRepairPurpose.OWNER_OBSERVATION
        assert evidence.original_document == journal_document(raw)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None and attempt.lease_deadline == deadline
        assert attempt.state != "running"
        assert set(session.scalars(select(AgentOperation.id))) == native_ids


def test_new_native_identity_after_discovery_aborts_declared_lock_set(
    faulty_install, monkeypatch
):
    from vonk_control import run_switch_journal_repair as repair_owner

    (
        sessions,
        _lifecycle,
        _profiles,
        _planner,
        jobs,
        clock,
        nodes,
        _application,
        switch_id,
        _install_id,
        claim,
        _identity,
        raw,
        _claims,
        native_ids,
    ) = faulty_install
    from vonk_control.models import AgentOperationAttempt

    with sessions() as session:
        before_attempts = set(session.scalars(select(AgentOperationAttempt.id)))
    lock_declared = repair_owner._lock_discovery
    changed = []

    def complete_current_native_before_lock(session, captured):
        # Real exact native completion emits the next role via its normal
        # producer after immutable discovery, before any declared row lock.
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": claim.fence,
                        "state": "succeeded",
                        "result": {"installed_bytes": 64},
                    }
                ),
                strict=True,
            )
        )
        # Main queues the ranks together. Claiming the next queued rank adds a
        # genuine attempt identity after discovery without changing the order IDs.
        assert claim_agent(jobs, nodes[1], "serial-1", 60) is not None
        changed.append(True)
        lock_declared(session, captured)

    monkeypatch.setattr(
        repair_owner, "_lock_discovery", complete_current_native_before_lock
    )
    assert (
        try_repair_zero_transfer_journal(sessions, switch_id, clock[0])
        == JournalRepairDisposition.DEFERRED
    )
    assert changed == [True]
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.result == raw
        assert set(session.scalars(select(AgentOperation.id))) == native_ids
        assert set(session.scalars(select(AgentOperationAttempt.id))) != before_attempts
        assert not list(session.scalars(select(RunSwitchJournalRepair)))


def test_finished_rank_lease_cannot_waive_live_rank_measurement_provenance(
    faulty_install,
):
    from vonk_control.models import AgentOperationAttempt

    (
        sessions,
        _lifecycle,
        _profiles,
        _planner,
        jobs,
        clock,
        nodes,
        _application,
        switch_id,
        install_id,
        claim,
        _identity,
        raw,
        _claims,
        _native_ids,
    ) = faulty_install
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": claim.fence,
                    "state": "succeeded",
                    "result": {"installed_bytes": 64},
                }
            ),
            strict=True,
        )
    )
    with sessions() as session:
        finished_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert finished_attempt is not None
        clock[0] = finished_attempt.lease_deadline - timedelta(seconds=10)
    live = claim_agent(jobs, nodes[1], "serial-1", 60)
    assert live is not None and live.operation == "recipe.install"
    with sessions() as session:
        finished = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        current = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == live.fence
            )
        )
        assert finished is not None and current is not None
        assert finished.state == LifecycleState.SUCCEEDED
        clock[0] = finished.lease_deadline + timedelta(microseconds=1)
        assert clock[0] < current.lease_deadline
    jobs.heartbeat(
        live,
        OperationProgress(
            phase="copying", completed_bytes=56, total_bytes=64, total_bytes_known=True
        ),
        60,
    )
    with sessions() as session:
        before_claims = [
            (item.id, item.state, item.amount_bytes)
            for item in session.scalars(select(ResourceReservation))
        ]
        before_native = set(session.scalars(select(AgentOperation.id)))
        child = session.get(Job, install_id)
        assert child is not None and child.state == LifecycleState.RUNNING
    assert (
        try_repair_zero_transfer_journal(sessions, switch_id, clock[0])
        == JournalRepairDisposition.DEFERRED
    )
    with sessions() as session:
        row = session.get(Job, switch_id)
        assert row is not None and row.result == raw
        assert [
            (item.id, item.state, item.amount_bytes)
            for item in session.scalars(select(ResourceReservation))
        ] == before_claims
        assert set(session.scalars(select(AgentOperation.id))) == before_native
        assert not list(session.scalars(select(RunSwitchJournalRepair)))
        current = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == live.fence
            )
        )
        assert current is not None and current.state == LifecycleState.RUNNING


@pytest.mark.parametrize(
    "fault",
    ["missing-attempt", "wrong-child-request", "missing-child", "retained-evidence"],
)
@pytest.mark.parametrize("cancel", [False, True])
def test_unproven_repair_ends_without_blocking_fresh_same_spark(
    faulty_install, fault, cancel
):
    """Catches endless DEFERRED holds and cancel refusal before intent is saved."""
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import delete
    from vonk_control.models import AgentOperationAttempt
    from vonk_control.run_switch_contract import RunSwitchApplyRequest
    from vonk_control.run_switch_journal_contract import (
        JournalRepairCode,
        RunSwitchJournalRepairPendingState,
    )

    from .non_blocking import assert_ended_without_blocking

    (
        sessions,
        _lifecycle,
        _profiles,
        planner,
        _jobs,
        clock,
        nodes,
        _application,
        switch_id,
        install_id,
        claim,
        _identity,
        raw,
        _claims,
        _native_ids,
    ) = faulty_install
    if fault == "retained-evidence":
        # A valid historical witness can still belong to a different exact
        # request. Preserve it; expiry must append an end without a unique-key
        # failure rolling back the release of all holds.
        assert (
            try_repair_zero_transfer_journal(sessions, switch_id, clock[0])
            == JournalRepairDisposition.REPAIRED
        )
        with sessions() as session:
            [audit] = list(session.scalars(select(RunSwitchJournalRepair)))
            original_digest = audit.original_digest
            mismatched = RunSwitchJournalRepairEvidence.model_validate_json(
                canonical_message(audit.evidence), strict=True
            ).model_copy(update={"request_key": str(uuid4())})
    with sessions.begin() as session, write_guard_mode(strict=False):
        if fault == "missing-attempt":
            session.execute(
                delete(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == claim.fence
                )
            )
        elif fault == "retained-evidence":
            session.execute(
                delete(RunSwitchJournalRepair).where(
                    RunSwitchJournalRepair.job_id == switch_id
                )
            )
            job = session.get(Job, switch_id)
            assert job is not None
            job.result = raw
            session.add(
                RunSwitchJournalRepair(
                    job_id=switch_id,
                    original_digest=original_digest,
                    evidence=mismatched,
                    created_at=clock[0],
                )
            )
        elif fault == "missing-child":
            session.execute(delete(Job).where(Job.id == install_id))
        else:
            child = session.get(Job, install_id)
            assert child is not None
            child.request_id = str(uuid4())
    assert not planner.tick()
    with sessions() as session:
        pending = session.get(RunSwitchJournalRepairPending, switch_id)
        assert pending is not None
        first = RunSwitchJournalRepairPendingState.model_validate_json(
            canonical_message(pending.progress), strict=True
        )
    assert first.attempts == 1 and first.next_attempt_at > clock[0]
    assert planner.get(switch_id).next_attempt_at == first.next_attempt_at
    activity = planner.activity_provider().get_operation(switch_id)
    assert activity["supported_actions"] == ["cancel"]
    # No busy loop or deadline reset at the same clock, including after restart.
    assert not planner.tick()
    if cancel:
        accepted = planner.cancel(
            switch_id,
            actor="admin",
            request_key=str(uuid4()),
            reason="cancel while evidence is missing",
        )
        assert accepted.state != "failed"
        with sessions() as session:
            pending = session.get(RunSwitchJournalRepairPending, switch_id)
            assert pending is not None
            saved = RunSwitchJournalRepairPendingState.model_validate_json(
                canonical_message(pending.progress), strict=True
            )
            assert (
                saved.cancellation is not None
                and saved.deadline_at == first.deadline_at
            )
    # Recreate the consumer to prove retry deadlines are PostgreSQL-owned.
    _profiles, planner = _measured_profile_service(sessions, _lifecycle)
    clock[0] = first.next_attempt_at
    assert not planner.tick()
    with sessions() as session:
        pending = session.get(RunSwitchJournalRepairPending, switch_id)
        assert pending is not None
        second = RunSwitchJournalRepairPendingState.model_validate_json(
            canonical_message(pending.progress), strict=True
        )
        assert second.attempts >= 2 and second.deadline_at == first.deadline_at
        job = session.get(Job, switch_id)
        assert job is not None
        parent = owner._run_switch_payload(job)
        assert parent is not None
        assert isinstance(parent.intent, RunSwitchRunIntent)
        request = parent.intent.request
    clock[0] = first.deadline_at

    def end(_operation):
        assert planner.tick()
        return planner.get(switch_id)

    def fresh(_world):
        return planner.apply(
            RunSwitchApplyRequest.model_validate_json(
                canonical_message(
                    request.model_copy(
                        update={"request_key": str(uuid4()), "plan_digest": None}
                    )
                ),
                strict=True,
            ),
            actor="admin",
        )

    def reason(ended):
        assert ended.status_reason.startswith(JournalRepairCode.EXHAUSTED)

    ended, admitted = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        planner.get(switch_id),
        end=end,
        fresh=fresh,
        assert_reason=reason,
    )
    if cancel:
        assert ended.state == "cancelled"
    assert admitted.node_ids == list(nodes)
    with sessions() as session:
        job = session.get(Job, switch_id)
        assert job is not None and job.result == raw
        assert session.get(RunSwitchJournalRepairPending, switch_id) is None
        from vonk_control.run_switch_journal_contract import (
            RunSwitchJournalRepairEndEvidence,
        )

        [audit] = list(
            session.scalars(
                select(RunSwitchJournalRepair).where(
                    RunSwitchJournalRepair.record_kind == "end"
                )
            )
        )
        if fault == "retained-evidence":
            [prior] = list(
                session.scalars(
                    select(RunSwitchJournalRepair).where(
                        RunSwitchJournalRepair.record_kind == "repair"
                    )
                )
            )
            assert prior.evidence == mismatched
        retained = RunSwitchJournalRepairEndEvidence.model_validate_json(
            canonical_message(audit.evidence), strict=True
        )
        assert retained.original_document == journal_document(raw)
        assert (retained.cancellation is not None) == cancel
        assert not list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.state.in_(["active", "promised"])
                )
            )
        )


def test_cancel_under_native_repair_contention_persists_before_proof(faulty_install):
    """A NOWAIT repair refusal cannot refuse or erase the accepted cancel."""
    from types import SimpleNamespace
    from uuid import uuid4

    from psycopg.errors import LockNotAvailable
    from sqlalchemy import event
    from sqlalchemy.engine import ExceptionContext
    from vonk_control.job_documents import RunSwitchRunIntent
    from vonk_control.run_switch_journal_contract import (
        RunSwitchJournalRepairPendingState,
    )

    from .non_blocking import assert_ended_without_blocking

    (
        sessions,
        _lifecycle,
        _profiles,
        planner,
        _jobs,
        clock,
        _nodes,
        _application,
        switch_id,
        install_id,
        _claim,
        _identity,
        _raw,
        _claims,
        _native_ids,
    ) = faulty_install
    with sessions() as session:
        job = session.get(Job, switch_id)
        assert job is not None
        parent = owner._run_switch_payload(job)
        assert parent is not None and isinstance(parent.intent, RunSwitchRunIntent)
        request = parent.intent.request
    holder = sessions()
    holder.begin()
    assert (
        holder.scalar(
            select(AgentOperation)
            .where(AgentOperation.parent_job_id == install_id)
            .with_for_update()
        )
        is not None
    )
    request_key = str(uuid4())
    refused = False

    def observe_native_refusal(context: ExceptionContext) -> None:
        nonlocal refused
        if not isinstance(context.original_exception, LockNotAvailable):
            return
        assert context.statement is not None
        assert "agent_operations" in context.statement
        assert "FOR UPDATE OF agent_operations NOWAIT" in context.statement
        # Observe the real database refusal, while the holder still owns the
        # native row. A separate connection must already see committed intent;
        # persisting it after repair (or only on the same transaction) fails.
        with sessions() as session:
            row = session.get(RunSwitchJournalRepairPending, switch_id)
            assert row is not None
            state = RunSwitchJournalRepairPendingState.model_validate_json(
                canonical_message(row.progress), strict=True
            )
            assert state.cancellation is not None
            assert state.cancellation.request_key == request_key
        refused = True

    engine = holder.get_bind()
    event.listen(engine, "handle_error", observe_native_refusal)
    try:
        # The lock acquisition above is the synchronization point. No worker
        # scheduling or one-second wall-clock race is needed: NOWAIT itself
        # proves nonblocking contention under the owning database budgets.
        accepted = planner.cancel(
            switch_id,
            actor="admin",
            request_key=request_key,
            reason="cancel under native contention",
        )
        assert refused
        assert accepted.operation_id == switch_id
        with sessions() as session:
            row = session.get(RunSwitchJournalRepairPending, switch_id)
            assert row is not None
            pending = RunSwitchJournalRepairPendingState.model_validate_json(
                canonical_message(row.progress), strict=True
            )
            assert pending.cancellation is not None
    finally:
        event.remove(engine, "handle_error", observe_native_refusal)
        holder.rollback()
        holder.close()
    clock[0] = pending.deadline_at

    def end(_receipt):
        assert planner.tick()
        return planner.get(switch_id)

    ended, _fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        accepted,
        end=end,
        fresh=lambda _world: planner.apply(
            request.model_copy(
                update={"request_key": str(uuid4()), "plan_digest": None}
            ),
            actor="admin",
        ),
    )
    assert ended.state == LifecycleState.CANCELLED.value
