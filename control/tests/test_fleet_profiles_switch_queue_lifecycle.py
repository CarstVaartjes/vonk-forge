"""Fleet profiles: switch queue lifecycle."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode
from vonk_agent_protocol.reason_codes.runs import SupersedeCode
from vonk_agent_protocol.state_machines import DesiredAssignmentState
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignment,
    FleetProfileInput,
    FleetProfileNode,
)
from vonk_control.fleet_profile_contract.effects import (
    FleetProfileRunEffect,
)
from vonk_control.fleet_profile_contract.switch_state import (
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchQueueItem,
)
from vonk_control.fleet_profiles.assessment_support import _operation_state
from vonk_control.fleet_profiles.factory import build_production_fleet_profile_service
from vonk_control.fleet_profiles.run_switch_adapter import RunSwitchFleetProfileAdapter
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.models.fleet import (
    FleetProfileApplication,
)
from vonk_control.models.jobs import Job
from vonk_control.run_switch_contract.plans import RunSwitchAssessment

from .fleet_profiles_support import (
    NOW,
    _assessment,
    _database,
    _exact_cleanup_profile,
    _exact_preparation,
    _follow_profile_retry,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)


def test_profile_preview_projects_exact_preparation_from_run_switch_authority(
    tmp_path: Path,
) -> None:
    """Regression: the production composition must project real preparation.

    Production binds ``RunSwitchFleetProfileAdapter.assess`` as the
    service's preparation provider.  When that binding is missing the preview
    reported ``allowed`` with an empty ``preparations`` list, which is the
    defect the Spark acceptance canary surfaced.
    """

    from vonk_control.run_switch_operations.service import RunSwitchOperationService

    from .recipe_operations_support import setup_services
    from .run_switch_operations_support import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    service = build_production_fleet_profile_service(
        sessions,
        clock=lifecycle._clock,
        run_switch_operations=run_switch,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Preview preparation",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "preview-preparation",
                    }
                ],
            }
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    assert preview.allowed is True
    assert len(preview.preparations) == 1
    preparation = preview.preparations[0].preparation
    assert tuple(preparation.target_node_ids) == tuple(sorted(nodes))
    assert preparation.model.artifact_set_sha256
    assert preparation.model.model_content_sha256
    assert preparation.runtime_image.image_digest.startswith("sha256:")
    assert preparation.runtime_image.oci_layout_sha256


def test_child_operation_state_unknown_is_observed_never_refused() -> None:
    """A stored state outside the contract is unknown, and unknown is observed.

    The caller's default is the observing state (``running``): a malformed value
    never refuses the read and never reads as a terminal state it did not reach.
    """

    assert _operation_state(None, default=LifecycleState.RUNNING) == "running"
    assert _operation_state("succeeded", default=LifecycleState.QUEUED) == "succeeded"
    assert _operation_state("not-a-state", default=LifecycleState.RUNNING) == "running"
    assert _operation_state(7, default=LifecycleState.RUNNING) == "running"


def test_switch_queue_removes_an_installation_the_profile_no_longer_references(
    tmp_path: Path,
) -> None:
    """The orchestrator, not the profile layer, performs the removal."""

    sessions, _lifecycle, adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path)
    )
    reviewed = service.preview(profile.id)
    with sessions() as session:
        exact, _ = adapter._plan_queue(
            session,
            (),
            tuple(nodes),
            application_id=_uuid(1235),
            installation_policy="exact",
            reviewed_effects=reviewed.effects,
            expected_images={},
        )
        retained, _ = adapter._plan_queue(
            session,
            (),
            tuple(nodes),
            application_id=_uuid(1236),
            installation_policy="keep-cached",
            reviewed_effects=reviewed.effects,
            expected_images={},
        )

    assert FleetProfileSwitchQueueItem(kind="cleanup", id=installed.owner_id) in exact
    # Retention decides whether it is removed at all: keep-cached retains it.
    assert retained == []


def test_profile_preview_delegates_removal_to_the_orchestrator(
    tmp_path: Path,
) -> None:
    """The profile states the intent and keeps it visible; Run/Switch removes it."""

    _sessions, _lifecycle, _adapter, service, profile, installed, _nodes = (
        _exact_cleanup_profile(tmp_path)
    )

    preview = service.preview(profile.id)

    assert [step.kind for step in preview.steps] == ["switch"]
    delegated = [
        reason
        for reason in preview.reasons
        if reason.code == "profile.cleanup_delegated"
    ]
    assert delegated, [reason.code for reason in preview.reasons]
    assert installed.owner_id in delegated[0].detail
    assert preview.summary.starts == 0
    assert preview.summary.uninstalls == 1


def _exact_planned_cleanup_profile(tmp_path: Path):
    """A production-wired exact profile whose only leftover is a persisted plan."""

    from vonk_control.run_switch_operations.service import RunSwitchOperationService

    from .recipe_operations_support import setup_services
    from .run_switch_operations_support import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    admission = lifecycle._install_admission
    plan = admission.plan_install(mapping_id, build_id, now=lifecycle._clock())
    assert plan.allowed, [
        (node.node_id, reason.code) for node in plan.nodes for reason in node.blockers
    ]
    planned = admission.accept_install(plan, actor="admin", now=lifecycle._clock())
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions, clock=lifecycle._clock, switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput(
            name="Exact planned cleanup",
            installation_policy="exact",
            assignments=[],
        ),
        actor="admin",
    )
    return sessions, lifecycle, adapter, run_switch, service, profile, planned, nodes


def test_exact_profile_switch_abandons_a_never_installed_leftover(
    tmp_path: Path,
) -> None:
    """A switch must not be blocked by a plan that was never installed.

    The cleanup candidate set still hands the superseded installation to
    Run/Switch, but the installation's own assessment resolves it as an
    abandonment, so the switch is admitted and the leftover is disposed of
    without node work.  Before the disposition existed this exact load failed
    with ``run-switch.plan_blocked: run-switch.uninstall-blocked``.
    """

    sessions, lifecycle, _adapter, run_switch, service, profile, planned, _nodes = (
        _exact_planned_cleanup_profile(tmp_path)
    )

    now = [lifecycle._clock()]
    lifecycle._clock = lambda: now[0]
    service._clock = lifecycle._clock
    run_switch._clock = lifecycle._clock

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]

    application = service.apply(
        profile.id,
        request_key=_uuid(910),
        actor="admin",
    )
    for _ in range(12):
        _follow_profile_retry(service, application.id, now)
        run_switch.tick()
        service.tick()
        if service.application(application.id).state in {"succeeded", "failed"}:
            break

    completed = service.application(application.id)
    assert completed.state == "succeeded", completed.status_reason
    # The profile application keeps the Run/Switch receipt, so the operator
    # sees the abandonment and its reason rather than a silent disappearance.
    from vonk_control.run_switch_contract.phase_results import RunSwitchUninstallResult

    receipts = [
        item.result.run_switch
        for item in completed.progress.step_results.values()
        if isinstance(item.result, FleetProfileSwitchChildResult)
    ]
    dispositions = [
        phase
        for receipt in receipts
        for phase in receipt.phase_results
        if isinstance(phase, RunSwitchUninstallResult)
    ]
    assert [phase.disposition for phase in dispositions] == ["abandoned"]
    assert [phase.reason for phase in dispositions] == ["installation-not-installed"]
    with sessions() as session:
        installation = session.get(RecipeInstallation, planned)
        assert installation is not None
        assert installation.state == "uninstalled"
        assert all(
            node.state == "uninstalled"
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == planned
                )
            )
        )


def test_acceptance_cleanup_consumer_reads_the_complete_run_switch_result(
    tmp_path: Path,
    postgres_engine: Engine,
) -> None:
    """The packaged acceptance consumer follows delegated cleanup evidence."""

    from vonk_control.run_switch_operations.service import RunSwitchOperationService

    from .recipe_operations_support import (
        installed_recipe,
        setup_services,
        started_recipe,
    )
    from .run_switch_operations_support import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1, engine=postgres_engine
    )
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=_uuid(900)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=_uuid(901),
        alias="acceptance-cleanup",
    )
    now = [lifecycle._clock()]
    lifecycle._clock = lambda: now[0]
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput(
            name="Acceptance exact cleanup",
            installation_policy="exact",
            assignments=[],
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        request_key=_uuid(902),
        actor="admin",
    )
    completed_agent_operations: set[str] = set()
    for _ in range(30):
        _follow_profile_retry(service, application.id, now)
        run_switch.tick()
        service.tick()
        current = service.application(application.id)
        if current.state == "succeeded":
            break
        state = current.progress.switch_adapter
        assert state is not None
        for pending_child in state.pending_children:
            assert pending_child.kind == state.queue[pending_child.queue_index].kind
            child = run_switch.get(pending_child.operation_id)
            operation_id = child.result.child_operation_id if child.result else None
            if operation_id is None or operation_id in completed_agent_operations:
                continue
            with sessions() as session:
                operation = session.get(Job, operation_id)
                assert operation is not None
                kind = operation.kind
            assert kind in {"recipe.stop", "recipe.uninstall"}
            lifecycle.record_node_result(
                operation_id, nodes[0], succeeded=True, evidence={}
            )
            completed_agent_operations.add(operation_id)
    else:
        pytest.fail(
            "profile cleanup did not converge: "
            + json.dumps(current.model_dump(mode="json"), default=str)
        )

    completed = service.application(application.id)
    payload = {
        "preview": preview.model_dump(mode="json"),
        "application": completed.model_dump(mode="json"),
        "installation_id": installation.owner_id,
        "run_id": run.owner_id,
        "node_id": nodes[0],
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import copy
import json
import runpy
import sys

m = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")
value = json.load(sys.stdin)
m["_validate_canary_cleanup_preview"](
    value["preview"], node_id=value["node_id"]
)
m["_validate_canary_cleanup_application"](
    value["application"],
    installation_ids=[value["installation_id"]],
    run_id=value["run_id"],
)

def rejected(changed):
    try:
        m["_validate_canary_cleanup_application"](
            changed,
            installation_ids=[value["installation_id"]],
            run_id=value["run_id"],
        )
    except m["LifecycleError"]:
        return
    raise AssertionError("malformed cleanup evidence was accepted")

missing = copy.deepcopy(value["application"])
missing["progress"]["switch_adapter"]["result"]["children"].pop()
rejected(missing)
wrong_run = copy.deepcopy(value["application"])
children = wrong_run["progress"]["switch_adapter"]["result"]["children"]
stop = next(child for child in children if child["kind"] == "stop")
for receipt in stop["result"]["run_switch"]["phase_results"]:
    if receipt.get("phase") == "stop":
        receipt["run_id"] = "00000000-0000-4000-8000-000000000998"
rejected(wrong_run)
wrong = copy.deepcopy(value["application"])
children = wrong["progress"]["switch_adapter"]["result"]["children"]
cleanup = next(child for child in children if child["kind"] == "cleanup")
for receipt in cleanup["result"]["run_switch"]["phase_results"]:
    if "installation_id" in receipt:
        receipt["installation_id"] = "00000000-0000-4000-8000-000000000999"
rejected(wrong)
unverified = copy.deepcopy(value["application"])
children = unverified["progress"]["switch_adapter"]["result"]["children"]
cleanup = next(child for child in children if child["kind"] == "cleanup")
final = next(
    receipt for receipt in cleanup["result"]["run_switch"]["phase_results"]
    if receipt["phase"] == "final_verify"
)
final["final_verified"] = False
rejected(unverified)
""",
        ],
        cwd=Path(__file__).parents[2],
        input=json.dumps(payload),
        text=True,
        check=True,
    )


def test_persisted_child_progress_is_read_with_json_semantics() -> None:
    """Persisted JSON must be validated as JSON, not as Python objects.

    The database driver hands back decoded JSON, and the profile's own progress
    document stores ``start_deadline`` as the ISO string it serialized.  Reading
    that document in Python mode rejected it, and on the live fleet that failed
    a whole profile application after the distribution and install had already
    succeeded: "FleetProfileChildProgress -> start_deadline -> Input should be a
    valid datetime".
    """

    state = FleetProfileSwitchAdapterState.model_validate_json(
        json.dumps(
            {
                "child_id": _uuid(900),
                "scope_node_ids": [_node_id(1)],
                "assignment_ids": [],
                "assignments": [],
                "queue": [],
                "actor": "admin",
                "request_id": _uuid(901),
                "state": "running",
                "child_progress": {
                    "phase": "start",
                    "node_ids": [_node_id(1)],
                    "startup_budget_seconds": 1800,
                    "start_deadline": "2026-09-17T08:48:21.262460Z",
                },
            }
        )
    )

    view = RunSwitchFleetProfileAdapter._view_from_state(
        FleetProfileApplication(id=_uuid(900), wait_evidence=None, next_action_at=None),
        state,
    )

    progress = view.progress
    assert progress is not None
    assert progress.phase == "start"
    start_deadline = progress.start_deadline
    assert start_deadline is not None
    assert start_deadline.year == 2026
    assert start_deadline.microsecond == 262460


def test_a_replaced_workload_keeps_serving_until_its_successor_starts() -> None:
    """A replacement must prepare before stopping the exact old workload."""
    from vonk_control.fleet_profiles.projection_support import _switch_queue

    identities = {
        name: _uuid(19890 + index)
        for index, name in enumerate(
            ("old-glm", "spread", "new-glm", "solo-three", "solo-four", "leftover")
        )
    }

    def stop(name: str, *nodes: int) -> FleetProfileRunEffect:
        return FleetProfileRunEffect(
            run_id=identities[name],
            installation_id=_uuid(19899),
            alias=name,
            node_ids=[_node_id(node) for node in nodes],
            action="stop",
        )

    def assignment(name: str, *nodes: int) -> FleetProfileAssignment:
        return FleetProfileAssignment(
            id=identities[name],
            recipe_revision_id=_uuid(19898),
            recipe_id=_uuid(19897),
            recipe_title=name,
            topology_name="full-reviewed-group",
            desired_state=DesiredAssignmentState.RUNNING,
            alias=name,
            nodes=[
                FleetProfileNode(
                    node_id=_node_id(node),
                    rank=index,
                    role=f"rank-{index}",
                    endpoint_owner=index == 0,
                )
                for index, node in enumerate(nodes)
            ],
        )

    queue = _switch_queue(
        [stop("old-glm", 1, 2), stop("spread", 3, 4)],
        [
            assignment("new-glm", 1, 2),
            assignment("solo-three", 3),
            assignment("solo-four", 4),
        ],
        [identities["leftover"]],
    )
    assert [(item.kind, item.id) for item in queue] == [
        # Neither single-node successor alone replaces the old two-node gang.
        ("stop", identities["spread"]),
        ("run", identities["new-glm"]),
        ("run", identities["solo-three"]),
        ("run", identities["solo-four"]),
        # This successor keeps the complete old group serving while preparing.
        ("stop", identities["old-glm"]),
        ("cleanup", identities["leftover"]),
    ]


def test_a_stop_step_for_an_already_replaced_workload_is_skipped(
    tmp_path: Path,
) -> None:
    from vonk_control.run_switch_operations.service import RunSwitchOperationService

    from .recipe_operations_support import (
        installed_recipe,
        setup_services,
        started_recipe,
    )
    from .run_switch_operations_support import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=_uuid(652)
    )
    run = started_recipe(
        sessions, lifecycle, installation.owner_id, nodes, request_id=_uuid(653)
    )
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )
    service.preview(profile.id)
    application = service.apply(profile.id, request_key=_uuid(654), actor="admin")
    # Its replacement already stopped it before this step was reached.
    with sessions.begin() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        stored.state = "stopped"

    service.tick()
    service.tick()

    current = service.application(application.id)
    state = current.progress.switch_adapter
    assert state is not None
    assert not state.pending_children
    assert current.state == "succeeded", current.status_reason


def test_every_application_state_is_active_or_named_ended() -> None:
    """A new terminal state must join the one ended-state set every consumer uses."""

    from typing import get_args

    from vonk_control.fleet_profile_contract.vocabulary import (
        FLEET_PROFILE_ENDED_STATES,
        FleetProfileOperationState,
    )

    active = {"queued", "running", "backoff"}
    assert {
        str(state) for state in get_args(FleetProfileOperationState)
    } == active | set(FLEET_PROFILE_ENDED_STATES)


def test_a_superseded_application_names_its_reason_and_never_a_failure() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    view = service.apply(profile.id, request_key=_uuid(981), actor="admin")
    ended = view.model_copy(update={"state": "superseded"})
    with pytest.raises(ValueError):
        type(view).model_validate(ended.model_dump())
    named = ended.model_copy(
        update={
            "reason_code": SupersedeCode.SUPERSEDED_BY_RETRY,
            "superseded_by": _uuid(982),
        }
    )
    type(view).model_validate(named.model_dump())
    # A retry supersession must name its successor; others may not name a failure.
    with pytest.raises(ValueError):
        type(view).model_validate(
            named.model_copy(update={"superseded_by": None}).model_dump()
        )
    with pytest.raises(ValueError):
        type(view).model_validate(
            view.model_copy(update={"superseded_by": _uuid(982)}).model_dump()
        )


@pytest.mark.parametrize(
    "end_path", ["admission", "cache-recovery", "conflict-recovery"]
)
def test_exhausted_preparation_ends_and_a_fresh_load_is_admitted(
    monkeypatch, end_path: str
) -> None:
    """An exhausted chain must release its application scope, not poison new loads."""
    from vonk_agent_protocol.reason_codes.recipes import RecipeImageCode
    from vonk_agent_protocol.state_machines import ReservationState
    from vonk_control.models.fleet import FleetProfileApplication
    from vonk_control.operations.blockers import make_blocker

    from .non_blocking import assert_ended_without_blocking

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    scopes: list[str | None] = []

    def missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    def prepare(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        scopes.append(application_id)
        code = (
            RecipeImageCode.PREPARATION_EXHAUSTED
            if len(scopes) == (1 if end_path == "admission" else 2)
            else RecipeImageCode.PREPARING
        )
        # A bounded UI projection must retain the ending reason even when many
        # missing assets precede it; otherwise a restart would retry this end.
        noise = [
            make_blocker(
                RecipeImageCode.PREPARING,
                "Another asset is preparing",
                severity="error",
                node_ids=[_uuid(1000 + index)],
            )
            for index in range(32)
        ]
        return [
            *noise,
            make_blocker(code, "Bounded preparation result", severity="error"),
        ]

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        assessment_provider=missing,
        switch_adapter=_SwitchAdapter(),
    )
    service.bind_preparation_starter(prepare)
    profile = service.create(_input(revision_id), actor="admin")
    ended = service.apply(profile.id, request_key=_uuid(920), actor="admin")
    if end_path != "admission":
        from vonk_control.fleet_profiles.contracts import (
            FleetProfileAdmissionEffectBusy,
            _FleetProfileRecoveryBindingConflict,
        )

        # Automatic recovery scans backing-off loads, rather than pending admission.
        with sessions.begin() as session:
            row = session.get(FleetProfileApplication, ended.id)
            assert row is not None
            row.state = LifecycleState.BACKOFF

        for method in (
            "_reconcile_selected_roster",
            "_observe_pending_admissions",
            "_observe_pending_cancellation",
        ):
            monkeypatch.setattr(service, method, lambda now, application_id=None: False)
        monkeypatch.setattr(
            service,
            "_automatic_profile_recovery",
            lambda now, application_id=None: (ended.id, "admin"),
        )
        monkeypatch.setattr(service, "_retry_eligible", lambda session, row: True)
        error = (
            _FleetProfileRecoveryBindingConflict
            if end_path == "cache-recovery"
            else FleetProfileAdmissionEffectBusy
        )

        def interrupted(*args, **kwargs):
            raise error("missing assets during automatic recovery")

        monkeypatch.setattr(service, "retry", interrupted)
        assert service.tick()
        ended = service.application(ended.id)

    def released() -> None:
        with sessions() as session:
            row = session.get(FleetProfileApplication, ended.id)
            assert row is not None
            assert not row.progress["admission_pending"]
            assert row.progress.get("admission_retry_at") is None
            assert row.progress.get("retry_due_at") is None
            assert row.current_operation_id is None
            from sqlalchemy import select

            from vonk_control.models.execution import ResourceReservation

            assert (
                list(
                    session.scalars(
                        select(ResourceReservation).where(
                            ResourceReservation.owner_kind == "fleet-profile",
                            ResourceReservation.owner_id == ended.id,
                            ResourceReservation.state.in_(
                                (ReservationState.ACTIVE, ReservationState.PROMISED)
                            ),
                        )
                    )
                )
                == []
            )
        assert ended.next_attempt_at is None

    ended, fresh = assert_ended_without_blocking(
        None,
        ended,
        end=lambda view: view,
        assert_released=released,
        fresh=lambda _: service.apply(
            profile.id, request_key=_uuid(921), actor="admin"
        ),
    )
    assert scopes == [ended.id] * (1 if end_path == "admission" else 2) + [fresh.id]
    assert fresh.id != ended.id


@pytest.mark.parametrize("initially_waiting", [False, True])
def test_planner_validation_defect_ends_load_with_evidence_after_restart(
    initially_waiting: bool,
) -> None:
    """Catch internal contract errors being requeued as missing cache evidence."""
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    inject_defect = not initially_waiting
    cache_missing = initially_waiting
    calls: list[str] = []
    clock = [NOW]

    def assess(_session, _assignment, _nodes, **_kwargs):
        calls.append(_assignment.id)
        valid = _assessment(_exact_preparation((_node_id(1),)))
        if inject_defect:
            return RunSwitchAssessment.model_validate(
                {**valid.model_dump(), "allowed": False}
            )
        if cache_missing:
            raise ValueError("runtime image is still preparing")
        return valid

    def service():
        return FleetProfileService(
            sessions,
            clock=lambda: clock[0],
            assessment_provider=assess,
            switch_adapter=_SwitchAdapter(),
        )

    owner = service()
    profile = owner.create(_input(revision_id), actor="admin")
    application = owner.apply(profile.id, request_key=_uuid(930), actor="admin")
    if initially_waiting:
        assert application.state == LifecycleState.QUEUED
        inject_defect = True
        clock[0] += timedelta(minutes=1)
        owner = service()
        assert owner.tick()
        application = owner.application(application.id)
    assert len(calls) == (2 if initially_waiting else 1)
    assert application.state == LifecycleState.FAILED
    reason = next(
        item
        for item in application.progress.blockers
        if item.code == ProfileReasonCode.ADMISSION_DEFECT
    )
    assert "RunSwitchAssessment" in reason.detail
    assert "admission verdict" in reason.detail
    assert application.next_attempt_at is None
    observed_calls = len(calls)
    restarted = service()
    for _ in range(3):
        restarted.tick()
    assert len(calls) == observed_calls
    assert restarted.application(application.id).state == LifecycleState.FAILED
    inject_defect = cache_missing = False

    def assert_defect(view) -> None:
        assert any(
            item.code == ProfileReasonCode.ADMISSION_DEFECT
            for item in view.progress.blockers
        )

    ended, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        application,
        end=lambda view: view,
        assert_reason=assert_defect,
        fresh=lambda _: restarted.apply(
            profile.id, request_key=_uuid(931), actor="admin"
        ),
    )
    assert fresh.id != ended.id
    assert not fresh.progress.admission_pending
