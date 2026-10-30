"""An upgrade never stops a load in progress: retired plan fields are dropped on read."""

import copy
import json
import uuid
from typing import Any

from sqlalchemy import select, text
from vonk_control.models import Job, RecipeRun
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchOperationResult,
    RunSwitchStartResult,
)
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.run_switch_operations.observation_helpers import (
    _start_child_request_key,
)

from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _ObservingLifecycle,
    _request,
)

FILE_DIGESTS = [f"{index + 1:064x}" for index in range(300)]


def _old_verify_receipt() -> dict[str, object]:
    return {
        "phase": "verify",
        "subphase": "target-copy",
        "skipped": False,
        "verified": True,
        "verified_digests": FILE_DIGESTS[:1],
        "verified_build_id": None,
        "verified_image_digest": "sha256:" + "d" * 64,
        "verified_oci_layout_sha256": "e" * 64,
        "cached_nodes": [],
        "cached_target_totals": {},
        "evidence": [],
    }


def test_old_shape_job_with_a_starting_run_continues_to_success(tmp_path) -> None:
    """The plan and receipts carry the retired file lists; the load is not stopped."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=0,
        lifecycle=_ObservingLifecycle(lifecycle, healthy=True),
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    start_index = next(i for i, p in enumerate(plan.phases) if p.kind == "start")
    assert operation.result is not None
    ordinal = operation.result.workload_intent_ordinal
    assert ordinal is not None
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        request_id = row.request_id
    run_plan = lifecycle.preview_run(installation.owner_id, "qwen")
    started = lifecycle.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        # The start this operation issued: its deterministic request key.
        request_id=_start_child_request_key(request_id),
        workload_intent_ordinal=ordinal,
    )
    result = RunSwitchOperationResult(
        workload_intent_ordinal=ordinal,
        phase_index=start_index,
        item_index=0,
        phase="start",
        completed_phases=[phase.kind for phase in plan.phases[:start_index]],
        child_operation_id=started.id,
        phase_results=[RunSwitchStartResult(phase="start", run_id=started.owner_id)],
    ).model_dump(mode="json")
    with sessions.begin() as session:
        # As the interrupted agent left it: the launch parked, its run observed.
        child = session.get(Job, started.id)
        assert child is not None
        child.state = "waiting-for-operator"
        row = session.get(Job, operation.operation_id)
        assert row is not None
        payload: Any = copy.deepcopy(row.payload)
        # The shape before the upgrade: file lists on the plan and on a receipt.
        payload["plan"]["storage"]["artifact_digests"] = FILE_DIGESTS
        payload["plan"]["storage"]["reclaimable_digests"] = []
        result["phase_results"].insert(0, _old_verify_receipt())
        payload["progress"] = result
        session.flush()
        # Raw SQL: the typed columns refuse to write the retired shape.
        session.execute(
            text(
                "update jobs set payload = :payload, result = :result,"
                " state = 'running' where id = :id"
            ),
            {
                "payload": json.dumps(payload),
                "result": json.dumps(result),
                "id": operation.operation_id,
            },
        )

    for _ in range(8):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()
        service.redrive(operation.operation_id)

    view = service.get(operation.operation_id)
    assert view.state == "succeeded", view.status_reason
    assert "final_verify" in (view.result.completed_phases if view.result else [])
    with sessions() as session:
        assert not list(
            session.scalars(select(Job).where(Job.kind == "recipe.stop"))
        ), "the upgrade stopped the load it was observing"
        run = session.scalars(select(RecipeRun)).one()
        assert run.id == started.owner_id
        assert run.state not in {"stopping", "stopped"}
