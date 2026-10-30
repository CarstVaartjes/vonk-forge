"""An upgrade never stops a load in progress: an old-shape job re-plans from its intent."""

import copy
import json
import uuid
from typing import Any

from sqlalchemy import select, text
from vonk_control.artifact_reference_scan.intents import _run_switch_plan
from vonk_control.lifecycle.types import LifecycleState
from vonk_control.models import Job
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_operations.observation_helpers import (
    _start_child_request_key,
)
from vonk_control.run_switch_operations.planning_helpers import _run_switch_payload

from .test_recipe_operations import setup_services
from .test_run_switch_operations import (
    NOW,
    RecordingArtifactExecutor,
    _request,
    _service,
)

FILE_DIGEST = "a" * 64


def _old_verify_receipt() -> dict[str, object]:
    return {
        "phase": "verify",
        "subphase": "target-copy",
        "skipped": False,
        "verified": True,
        "verified_digests": [FILE_DIGEST],
        "verified_build_id": None,
        "verified_image_digest": "sha256:" + "d" * 64,
        "verified_oci_layout_sha256": "e" * 64,
        "cached_nodes": [],
        "cached_target_totals": {},
        "evidence": [],
    }


def _old_shape_job(tmp_path):
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    service = _service(
        sessions, NOW, lifecycle, RecordingArtifactExecutor(), phase_executor=None
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="operator")
    parent = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            request_key=str(uuid.uuid4()),
            plan_digest=plan.plan_digest,
        ),
        actor="operator",
    )
    start_id = str(uuid.uuid4())
    with sessions.begin() as session:
        job = session.get(Job, parent.operation_id)
        assert job is not None
        payload: Any = copy.deepcopy(job.payload)
        # The shape before the upgrade: a file list on the plan and on the receipt.
        payload["plan"]["storage"]["artifact_digests"] = [FILE_DIGEST]
        payload["progress"]["phase_results"] = [_old_verify_receipt()]
        result: Any = copy.deepcopy(job.result)
        result["phase_results"] = [_old_verify_receipt()]
        session.flush()
        # Raw SQL: the typed columns refuse to write the retired shape.
        session.execute(
            text(
                "update jobs set payload = :payload, result = :result, state = :state"
                " where id = :id"
            ),
            {
                "payload": json.dumps(payload),
                "result": json.dumps(result),
                "state": LifecycleState.OBSERVING.value,
                "id": parent.operation_id,
            },
        )
        session.expire(job)
        session.add(
            Job(
                id=start_id,
                request_id=_start_child_request_key(job.request_id),
                kind="recipe.start",
                state=LifecycleState.RUNNING.value,
                actor="operator",
                authority_revision="a" * 64,
                targets=list(job.targets),
                payload_digest="a" * 64,
                payload={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        assert _run_switch_payload(job) is None  # the old shape no longer reads

    return sessions, service, parent, start_id


def test_old_shape_job_with_a_running_start_replans_and_keeps_the_start(
    tmp_path,
) -> None:
    sessions, service, parent, start_id = _old_shape_job(tmp_path)
    service.redrive(parent.operation_id)

    with sessions() as session:
        job = session.get(Job, parent.operation_id)
        assert job is not None
        assert job.state not in {
            LifecycleState.FAILED.value,
            LifecycleState.CANCELLED.value,
        }, job.status_reason
        healed = _run_switch_payload(job)
        assert healed is not None and healed.intent is not None
        stored: Any = job.payload
        assert "artifact_digests" not in stored["plan"]["storage"]
        # The reference scan reads the rewritten plan again.
        assert _run_switch_plan(job).plan_digest == healed.plan_digest
        start = session.scalars(select(Job).where(Job.id == start_id)).one()
        assert start.state == LifecycleState.RUNNING.value


def test_old_shape_job_waits_while_its_replan_is_blocked(tmp_path, monkeypatch) -> None:
    """A re-plan that cannot be previewed yet retries; it never rejects the job."""
    from vonk_control.run_admission import RunAdmissionBusy

    sessions, service, parent, start_id = _old_shape_job(tmp_path)

    def busy(*args, **kwargs):
        raise RunAdmissionBusy("inventory temporarily unreadable")

    monkeypatch.setattr(service, "preview", busy)
    for _ in range(2):
        service.redrive(parent.operation_id)
    with sessions() as session:
        job = session.get(Job, parent.operation_id)
        assert job is not None
        assert job.state == LifecycleState.OBSERVING.value, job.status_reason
        start = session.get(Job, start_id)
        assert start is not None and start.state == LifecycleState.RUNNING.value
