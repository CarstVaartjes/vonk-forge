"""A replaced availability executor cannot bind or dispatch a build child."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.bounded_json import require_mapping
from vonk_control.models import Job, User

from .test_build_cancellation_recovery import _evidence, _services
from .test_recipe_builds import _write_controller_build_receipt


@pytest.mark.parametrize(
    "boundary",
    ["resolve", "prepare_plan", "bound_plan", "dispatch", "checkpoint", "recover"],
)
@pytest.mark.parametrize("replacement", ["takeover", "cancel"])
def test_replaced_builder_preserves_current_intent_at_each_commit(
    tmp_path, postgres_engine, monkeypatch, boundary, replacement
):
    sessions, builds, operations, storage, now, node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    clock = [now]
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: clock[0],
    )
    service = production.service
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    parent = service.start(revision.id, actor="operator", request_id=str(uuid.uuid4()))
    if boundary == "bound_plan":
        # Reconstruct the durable builder checkpoint before child dispatch,
        # preserving the actual composition's compiled runtime expectations.
        with sessions.begin() as session:
            row = session.get(Job, parent.id)
            assert row is not None
            row.payload = dict(row.payload) | {
                "runtime": dict(require_mapping(row.payload["runtime"], "runtime"))
                | {
                    "builder_node_id": node,
                    "build_input_sha256": plan.build_input_sha256,
                },
                "build_input_sha256": plan.build_input_sha256,
                "identity_key": plan.build_input_sha256,
            }
    if boundary == "recover":
        assert service.run_pending(limit=1) == 1
        assert service.get(parent.id).state == "queued"
        with sessions.begin() as session:
            row = session.get(Job, parent.id)
            assert row is not None
            dependency = dict(
                require_mapping(row.payload["build_dependency"], "dependency")
            )
            dependency.pop("operation_id")
            row.payload = dict(row.payload) | {"build_dependency": dependency}
        clock[0] += timedelta(seconds=6)
    original = service.claim_pending(limit=1, owner_id="worker")[0]
    replaced = []
    late_plan_writes = []

    def takeover():
        assert not replaced
        claim = None
        if replacement == "takeover":
            with sessions.begin() as session:
                row = session.get(Job, parent.id)
                assert row is not None
                row.payload = dict(row.payload) | {
                    "claim_until": (clock[0] - timedelta(seconds=1)).isoformat()
                }
            claim = service.claim_pending(limit=1, owner_id="replacement")[0]
        else:
            service.cancel(
                parent.id,
                actor="operator",
                request_id=str(uuid.uuid4()),
                reason="fence the accepted builder attempt",
            )
        with sessions() as session:
            row = session.get(Job, parent.id)
            assert row is not None
            replaced.append(
                (claim, deepcopy(row.payload), row.current_attempt, row.state)
            )

    if boundary in {"resolve", "prepare_plan", "bound_plan"}:
        target = builds
        attribute = "prepare_plan" if boundary == "bound_plan" else boundary
    elif boundary in {"dispatch", "checkpoint"}:
        target = operations
        attribute = "build"
    else:
        target = service
        attribute = "_builder"
    method = getattr(target, attribute)

    def paused(*args, **kwargs):
        if boundary in {"dispatch", "recover"}:
            takeover()
            return method(*args, **kwargs)
        result = method(*args, **kwargs)
        takeover()
        return result

    with monkeypatch.context() as patch:
        persist = builds.persist_plan_in_session

        def record_persistence(*args, **kwargs):
            if replaced:
                late_plan_writes.append(True)
            return persist(*args, **kwargs)

        patch.setattr(builds, "persist_plan_in_session", record_persistence)
        patch.setattr(target, attribute, paused)
        service.run_claim(original)
    assert len(replaced) == 1
    assert not late_plan_writes
    current, payload, attempt, state = replaced[0]
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        assert row.current_attempt == attempt
        if replacement == "cancel":
            assert row.state in {"cancelling", "cancelled"}
            assert row.payload["cancellation"] == payload["cancellation"]
            if row.state == "cancelled":
                assert "claim_owner" not in row.payload
                assert "claim_until" not in row.payload
        else:
            assert row.state == state
            assert row.payload == payload
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        assert len(children) == int(boundary in {"checkpoint", "recover"})

    # The valid executor either dispatches the original intent or recovers the
    # child that committed before takeover. Both paths produce one effect.
    if replacement == "cancel":
        service.reconcile_cancellations()
        operations.reconcile_cancelled_builds()
        service.reconcile_cancellations()
        assert service.get(parent.id).state == "cancelled"
        parent = service.start(
            revision.id, actor="operator", request_id=str(uuid.uuid4())
        )
        current = service.claim_pending(limit=1, owner_id="fresh-intent")[0]
    assert current is not None
    service.run_claim(current)
    waiting = service.get(parent.id)
    assert waiting.state == "queued", waiting.failure
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.build.v1", Job.state != "cancelled"
                )
            )
        )
        assert len(children) == 1
        child_id = children[0].id
    receipt = _write_controller_build_receipt(
        storage,
        archive=b"claim-fenced builder receipt",
        image_digest="sha256:" + "b" * 64,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    operations.record_node_result(
        child_id,
        node,
        succeeded=True,
        evidence=_evidence(plan)
        | {
            "image_bytes": receipt.image_bytes,
            "oci_layout_sha256": receipt.oci_archive_sha256,
        },
    )
    clock[0] += timedelta(seconds=6)
    assert service.run_pending(limit=1) == 1
    finished = service.get(parent.id)
    assert finished.state == "succeeded", finished.failure


def test_parent_claim_is_held_until_child_admission_commits(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, builds, operations, _storage, now, _node, revision, _plan = _services(
        tmp_path, postgres_engine
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: now,
    )
    service = production.service
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    parent = service.start(revision.id, actor="operator", request_id=str(uuid.uuid4()))
    claim = service.claim_pending(limit=1, owner_id="worker")[0]
    admitted = threading.Event()
    resume = threading.Event()
    start = operations._start_build_in_session

    def paused_start(*args, **kwargs):
        admitted.set()
        assert resume.wait(timeout=5)
        return start(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(operations, "_start_build_in_session", paused_start)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.run_claim, claim)
            try:
                assert admitted.wait(timeout=5)
                # A guard evaluated in a separate transaction would allow this
                # independent writer to replace the claim before dispatch.
                with (
                    pytest.raises(OperationalError) as busy,
                    sessions.begin() as session,
                ):
                    session.get(Job, parent.id, with_for_update={"nowait": True})
                assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
            finally:
                resume.set()
            future.result(timeout=5)
    assert service.get(parent.id).state == "queued"
    with sessions() as session:
        child = session.scalars(select(Job).where(Job.kind == "recipe.build.v1")).one()
        row = session.get(Job, parent.id)
        assert row is not None
        assert (
            require_mapping(row.payload["build_dependency"], "dependency")[
                "operation_id"
            ]
            == child.id
        )
    service.cancel(
        parent.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel after child admission",
    )
    service.reconcile_cancellations()
    assert operations.reconcile_cancelled_builds()
    service.reconcile_cancellations()
    assert service.get(parent.id).state == "cancelled"
    assert operations.get(child.id).state == "cancelled"
