"""Receipt encoding regressions independent of the Linux Rust probe lane."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from vonk_agent_protocol import RecipeStartResult, RouteState, canonical_message
from vonk_control.bounded_json import require_mapping
from vonk_control.job_documents import (
    DistributedRecoveryMarker,
    RecipeStopParent,
    ServiceRunStopReview,
    controller_recipe_document,
)
from vonk_control.models import AgentOperation, Job
from vonk_control.recipe_lifecycle_contract import parse_recipe_lifecycle_result
from vonk_control.recipe_operations.contracts import RecipeOperationContext
from vonk_control.recipe_operations.observation_helpers import _RECIPE_PARENT_READERS
from vonk_control.stored_json import bindings
from vonk_control.strict_json import read_stored_model, serialize_json_value

from .test_recipe_operations import installed_recipe, setup_services, start_evidence


@pytest.mark.parametrize("nodes", [1, 2])
def test_start_receipts_keep_the_agent_canonical_encoding(tmp_path, nodes):
    """Catch full dumps adding optional nulls to persisted and public receipts."""
    sessions, service, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id="1" * 36
    )
    plan = service.preview_run(installation.owner_id, "canonical-receipt")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
    )
    for node_id in node_ids:
        with sessions() as session:
            child = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == start.id,
                    AgentOperation.node_id == node_id,
                )
            )
            assert child is not None
            receipt = RecipeStartResult.model_validate_json(
                canonical_message(start_evidence(child.payload))
            )
        wire = json.loads(canonical_message(receipt))
        view = service.record_node_result(
            start.id, node_id, succeeded=True, evidence=wire
        )
        with sessions() as session:
            job = session.get(Job, start.id)
            assert job is not None
            stored = require_mapping(job.result, "stored receipt")
            assert (
                require_mapping(stored["launch_evidence"], "launch receipts")[node_id]
                == wire
            )
        public = require_mapping(view.result, "public receipt")
        assert (
            require_mapping(public["launch_evidence"], "launch receipts")[node_id]
            == wire
        )
        assert view.result == json.loads(canonical_message(view.lifecycle_result))


@pytest.mark.parametrize("kind,reader", list(_RECIPE_PARENT_READERS.items()))
def test_controller_parent_documents_round_trip(kind, reader):
    """Catch canonical egress dropping defaults from a Controller parent write."""
    parent = reader.validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": "11111111-1111-4111-8111-111111111111",
                "plan_digest": "a" * 64,
            }
        )
    )
    stored = controller_recipe_document(parent)
    assert stored == parent.model_dump(mode="json")
    assert reader.validate_json(json.dumps(stored)) == parent
    from vonk_control.agent_jobs.stored import column_message

    row = Job(kind=kind, payload=stored)
    assert column_message(row, "payload") == canonical_message(parent)


def test_controller_recovery_context_round_trip():
    """Catch optional nulls lost inside a persisted recovery/Stop review tree."""
    marker = DistributedRecoveryMarker(
        schema_version=1,
        failed_rank=0,
        deadline="2026-10-08T12:00:00+00:00",
    )
    review = ServiceRunStopReview(
        stage="accepted",
        route_state=RouteState.PUBLISHED,
        run_generation=1,
        target_node_ids=["spk_" + "a" * 32],
        missing_node_ids=[],
    )
    parent = RecipeStopParent(
        schema_version=1,
        owner_kind="run",
        owner_id="run",
        plan_digest="a" * 64,
        recovery=marker,
        service_stop_review=review,
    )
    context = RecipeOperationContext(recovery=marker, service_stop_review=review)
    for document in (marker, review, context, parent):
        stored = controller_recipe_document(document)
        assert stored == document.model_dump(mode="json")
        assert read_stored_model(type(document), stored, from_json=True) == document


@pytest.mark.parametrize(
    "kind,document",
    [
        (
            "recipe.start",
            {"successful_nodes": [], "failed_nodes": [], "node_evidence": {}},
        ),
        ("recipe.start", {"node_evidence": {}}),
        (
            "recipe.start",
            {
                "cancel_requested": True,
                "cancel_request_id": "11111111-1111-4111-8111-111111111111",
                "cancel_actor": "admin",
                "reason": "cancelled by request",
            },
        ),
        ("recipe.stop", {"stopped": True}),
        ("recipe.job.activate.v1", {"activated": True}),
    ],
)
def test_lifecycle_receipts_round_trip_through_stored_union(kind, document):
    """Catch a receipt encoder producing a shape its actual column reader refuses."""
    receipt = parse_recipe_lifecycle_result(kind, document)
    stored = serialize_json_value(receipt)
    adapter = bindings()["jobs.result"].adapter_for(kind)
    assert adapter is not None
    parsed = adapter.validate_json(json.dumps(stored))
    assert parse_recipe_lifecycle_result(kind, parsed) == receipt
    assert stored == json.loads(canonical_message(receipt))


def test_profile_stop_authorization_documents_round_trip():
    """Catch partial serialization losing Stop scope or profile parent defaults."""
    import hashlib

    from vonk_agent_protocol import RecipeStopPayload
    from vonk_control.job_documents import OfflineStopIntent
    from vonk_control.profile_stop_authority import (
        JobRunStopScope,
        ProfileJobRunStopAuthorization,
        ProfileJobRunStopJob,
        ProfileJobRunStopPhaseItem,
        ProfileJobRunStopTarget,
        ProfileStopOwnerBinding,
    )

    identity = "11111111-1111-4111-8111-111111111111"
    node = "spk_" + "a" * 32
    stop = RecipeStopPayload(
        run_id=identity,
        target_runtime_id=identity,
        run_generation=1,
        installation_id=identity,
        recipe_revision_id=identity,
        mapping_id=identity,
        plan_digest="a" * 64,
        rank=0,
        role="entrypoint",
        recipe_content_sha256="a" * 64,
        stop_timeout_seconds=1,
        cancel_pending_start=True,
    )
    target = ProfileJobRunStopTarget(
        artifact_job_id=identity,
        source_job_id=identity,
        source_operation_id=identity,
        node_id=node,
        stop_payload_sha256=hashlib.sha256(canonical_message(stop)).hexdigest(),
    )
    scope = JobRunStopScope(
        schema_version=1,
        run_id=identity,
        installation_id=identity,
        recipe_revision_id=identity,
        mapping_id=identity,
        mapping_generation=1,
        run_generation=1,
        plan_digest="a" * 64,
        workload_intent_ordinal=1,
        run_node_ids=[node],
        reachable_node_ids=[node],
        stop_plan_digest="a" * 64,
        targets=[target],
    )
    owner = ProfileStopOwnerBinding(
        **scope.model_dump(exclude={"targets", "unissued_artifact_job_ids"}),
        profile_application_id=identity,
        profile_operation_id=identity,
        profile_digest="a" * 64,
        profile_plan_digest="a" * 64,
        profile_step=0,
    )
    authorization = ProfileJobRunStopAuthorization(
        **owner.model_dump(),
        targets=[target],
    )
    parent = ProfileJobRunStopJob(
        schema_version=1,
        owner_kind="run",
        owner_id=identity,
        plan_digest="a" * 64,
        workload_intent_ordinal=1,
        execution_mode="profile-jobrun-stop",
        profile_application_id=identity,
        profile_operation_id=identity,
        profile_stop_authorization=authorization,
        phases=[
            [
                ProfileJobRunStopPhaseItem(
                    operation_id=identity, node_id=node, payload=stop
                )
            ]
        ],
    )
    for document in (
        target,
        scope,
        owner,
        authorization,
        parent,
        OfflineStopIntent(node_ids=[node]),
    ):
        stored = controller_recipe_document(document)
        assert stored == document.model_dump(mode="json")
        assert read_stored_model(type(document), stored, from_json=True) == document
    stored_parent = require_mapping(
        controller_recipe_document(parent), "profile Stop parent"
    )
    assert ProfileJobRunStopJob.model_validate_parent(stored_parent) == parent
    assert (
        _RECIPE_PARENT_READERS["recipe.stop"].validate_json(json.dumps(stored_parent))
        == parent
    )


def test_jobrun_and_disposal_receipts_round_trip():
    """Catch canonical receipts omitting required nullable execution evidence."""
    from vonk_agent_protocol.recipe_jobs import (
        RecipeJobEvidence,
        RecipeJobOutputManifest,
        RecipeJobRunResult,
        manifest_sha256,
    )
    from vonk_control.recipe_operations.contracts import RecipeInstallationAbandonResult

    identity = "11111111-1111-4111-8111-111111111111"
    jobrun = RecipeJobRunResult(
        job_id=identity,
        run_id=identity,
        exit_code=0,
        output_manifest=RecipeJobOutputManifest(
            schema_version=1,
            total_bytes=0,
            files=(),
            manifest_sha256=manifest_sha256(()),
        ),
        evidence=RecipeJobEvidence(elapsed_milliseconds=0, peak_memory_bytes=None),
    )
    adapter = bindings()["jobs.result"].adapter_for("recipe.job.run.v1")
    assert adapter is not None
    assert adapter.validate_json(json.dumps(serialize_json_value(jobrun))) == jobrun
    assert (
        parse_recipe_lifecycle_result("recipe.job.run.v1", serialize_json_value(jobrun))
        == jobrun
    )
    disposal = RecipeInstallationAbandonResult(installation_id=identity)
    assert (
        RecipeInstallationAbandonResult.model_validate_json(
            json.dumps(serialize_json_value(disposal))
        )
        == disposal
    )
