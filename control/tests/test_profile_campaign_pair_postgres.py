"""A paired whole-fleet profile owns both recipe lanes and their receipts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileChildOperation,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchChildState,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfileApplication,
)
from vonk_control.run_switch_contract import (
    RunSwitchMemberReceipt,
    RunSwitchOperationResult,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .test_fleet_profile_api import _client, _headers
from .test_fleet_profiles import (
    NOW,
    _assessment,
    _exact_preparation,
    _recipe_document,
    _SwitchAdapter,
    _uuid,
)
from .test_fleet_profiles_canonical import (
    NODE_1,
    NODE_2,
    RECIPE_REVISION_ID,
    _seed,
)

_FIRST_RECIPE = "vonk-forge/synthetic-tiny-image"
_SECOND_RECIPE = "vonk-forge/synthetic-tiny-solo"


class _PairedReceiptAdapter(_SwitchAdapter):
    """Keep the real profile worker seam while returning two lane receipts."""

    def __init__(self) -> None:
        super().__init__()
        self.outer_operation_ids: dict[str, str] = {}

    def start(
        self,
        *,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
    ) -> FleetProfileChildOperation:
        started = super().start(
            application_id=application_id,
            assignments=assignments,
            scope_node_ids=scope_node_ids,
            actor=actor,
            request_id=request_id,
        )
        ordered = tuple(sorted(assignments, key=lambda item: item.id))
        assignment_ids = [item.id for item in ordered]
        child_receipts: list[FleetProfileSwitchChildState] = []
        for item_index, assignment in enumerate(ordered):
            operation_id = str(uuid4())
            run_switch = RunSwitchOperationResult(
                profile_application_id=application_id,
                item_index=item_index,
                phase="final_verify",
                completed_phases=["start", "final_verify"],
                members=[
                    RunSwitchMemberReceipt(
                        node_id=node.node_id,
                        phase="final_verify",
                        state="succeeded",
                    )
                    for node in assignment.nodes
                ],
            )
            child_receipts.append(
                FleetProfileSwitchChildState(
                    operation_id=operation_id,
                    kind="run",
                    state="succeeded",
                    result=FleetProfileSwitchChildResult(
                        run_switch_operation_id=operation_id,
                        run_switch=run_switch,
                    ),
                )
            )
        final = self._operations[started.id][-1]
        self._operations[started.id][-1] = final.model_copy(
            update={
                "result": FleetProfileSwitchAdapterResult(
                    children=child_receipts,
                    assignment_ids=assignment_ids,
                )
            }
        )
        self.outer_operation_ids[application_id] = started.id
        return started


def _add_second_recipe_revision(sessions: sessionmaker) -> str:
    document = _recipe_document()
    identity = document.get("identity")
    metadata = document.get("metadata")
    assert isinstance(identity, dict)
    assert isinstance(metadata, dict)
    identity["slug"] = "synthetic-tiny-solo"
    metadata["title"] = "Synthetic Tiny Solo"
    recipe = RecipeDefinition.model_validate(document)
    document_id = _uuid(50)
    revision_id = _uuid(51)
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=document_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.metadata.title,
                created_by="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=document_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=recipe.model_dump(mode="json"),
                content_digest=content_sha256(recipe),
                execution_key="c" * 64,
                created_by="test",
                created_at=NOW,
            )
        )
    return revision_id


@pytest.mark.lane
def test_postgres_paired_profile_has_one_owner_and_lane_attributed_receipts(
    postgres_engine, monkeypatch
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    second_revision_id = _add_second_recipe_revision(sessions)
    adapter = _PairedReceiptAdapter()
    profiles = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=adapter,
        assessment_provider=lambda _session, _assignment, expected_nodes, **_kwargs: (
            _assessment(_exact_preparation(tuple(expected_nodes)))
        ),
    )
    api, codec = _client(sessions, profiles=profiles)
    headers = _headers(codec, "administrator")
    saved = api.put(
        "/api/profile/1",
        headers=headers,
        json={
            "name": "Paired qualification batch",
            "expected_revision": 0,
            "assignments": [
                {
                    "recipe_selector": _FIRST_RECIPE,
                    "spark_ids": [NODE_1],
                    "assignment_name": "lane-one",
                    "desired_state": "running",
                },
                {
                    "recipe_selector": _SECOND_RECIPE,
                    "spark_ids": [NODE_2],
                    "assignment_name": "lane-two",
                    "desired_state": "running",
                },
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    review = api.post("/api/profile/1/preview", headers=headers)
    assert review.status_code == 200, review.text
    assert review.json()["allowed"] is True
    reviewed = review.json()["plan_digest"]

    path = "/api/profile/1/load"
    request_keys = [str(uuid4()), str(uuid4())]
    request_bodies = [
        {"request_key": key, "plan_digest": reviewed} for key in request_keys
    ]
    both_reviewed = Barrier(2)
    original_preview = FleetProfileService.preview

    def synchronize_review(service, *args, **kwargs):
        result = original_preview(service, *args, **kwargs)
        both_reviewed.wait(timeout=10)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(FleetProfileService, "preview", synchronize_review)

        lost_response_keys: list[str] = []

        def submit(body: dict[str, str]):
            response = api.post(path, headers=headers, json=body)
            if response.status_code == 202:
                # The API committed the application, but its response is lost
                # before the caller can observe it.
                lost_response_keys.append(body["request_key"])
                raise ConnectionError("simulated lost load response")
            return response

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(submit, body)
                for body in request_bodies
            ]
            responses = []
            for future in futures:
                try:
                    responses.append(future.result(timeout=15))
                except ConnectionError as error:
                    assert str(error) == "simulated lost load response"

    assert len(lost_response_keys) == 1
    assert [response.status_code for response in responses] == [409]
    rejected = responses[0]
    accepted_key = lost_response_keys[0]
    accepted_body = request_bodies[request_keys.index(accepted_key)]
    accepted = api.post(path, headers=headers, json=accepted_body)
    assert accepted.status_code == 202, accepted.text
    assert rejected.headers["x-vonk-error-code"] in {
        "controller.conflict",
        "profile.stale_plan",
    }
    assert accepted.json()["request_key"] == accepted_key
    application_id = accepted.json()["id"]
    assert accepted.json()["progress"]["intended_profile"]["scope"]["node_ids"] == [
        NODE_1,
        NODE_2,
    ]

    with sessions() as session:
        rows = list(session.scalars(select(FleetProfileApplication)))
        assert len(rows) == 1
        assert rows[0].id == application_id
        assert rows[0].request_key == accepted_key

    # Replaying the accepted key after a lost response must recover this pair.
    replayed = api.post(path, headers=headers, json=accepted_body)
    assert replayed.status_code == 202, replayed.text
    assert replayed.json() == accepted.json()

    for _ in range(10):
        completed = profiles.application(application_id)
        if completed.state == "succeeded":
            break
        assert profiles.tick() is True
    else:
        pytest.fail("paired application did not reach a terminal success")

    intended = completed.progress.intended_profile
    assert intended is not None
    assignments = {item.id: item for item in intended.assignments}
    assert {
        (item.recipe_revision_id, tuple(node.node_id for node in item.nodes))
        for item in assignments.values()
    } == {
        (RECIPE_REVISION_ID, (NODE_1,)),
        (second_revision_id, (NODE_2,)),
    }
    step = completed.progress.step_results["0"]
    assert step.operation_id == adapter.outer_operation_ids[application_id]
    result = step.result
    assert isinstance(result, FleetProfileSwitchAdapterResult)
    assert result.assignment_ids == sorted(assignments)
    assert len(result.children) == 2
    observed_lanes: set[tuple[str, str]] = set()
    receipt_ids: set[str] = set()
    for child in result.children:
        assert child.state == "succeeded"
        assert child.result is not None
        receipt = child.result
        assert isinstance(receipt, FleetProfileSwitchChildResult)
        assert receipt.run_switch_operation_id == child.operation_id
        run_switch = receipt.run_switch
        assert run_switch.profile_application_id == application_id
        assignment_id = result.assignment_ids[run_switch.item_index]
        assignment = assignments[assignment_id]
        assert [member.node_id for member in run_switch.members] == [
            node.node_id for node in assignment.nodes
        ]
        assert all(member.state == "succeeded" for member in run_switch.members)
        assert receipt.run_switch_operation_id not in receipt_ids
        receipt_ids.add(receipt.run_switch_operation_id)
        observed_lanes.add(
            (assignment.recipe_revision_id, run_switch.members[0].node_id)
        )
    assert observed_lanes == {
        (RECIPE_REVISION_ID, NODE_1),
        (second_revision_id, NODE_2),
    }

    # A profile edit makes a new request with the old review stale, while the
    # accepted request key continues to return its immutable two-lane result.
    edited = api.put(
        "/api/profile/1",
        headers=headers,
        json={
            "name": "Single lane after batch",
            "expected_revision": 1,
            "assignments": [
                {
                    "recipe_selector": _FIRST_RECIPE,
                    "spark_ids": [NODE_1],
                    "assignment_name": "lane-one",
                    "desired_state": "running",
                }
            ],
        },
    )
    assert edited.status_code == 200, edited.text
    stale_key = str(uuid4())
    stale = api.post(
        path,
        headers=headers,
        json={"request_key": stale_key, "plan_digest": reviewed},
    )
    assert stale.status_code == 409, stale.text
    assert stale.headers["x-vonk-error-code"] == "profile.stale_plan"
    with sessions() as session:
        rows = list(session.scalars(select(FleetProfileApplication)))
        assert len(rows) == 1
        assert session.scalar(
            select(FleetProfileApplication.id).where(
                FleetProfileApplication.request_key == stale_key
            )
        ) is None
    original = api.post(path, headers=headers, json=accepted_body)
    assert original.status_code == 202, original.text
    assert original.json()["id"] == application_id
    assert len(original.json()["progress"]["intended_profile"]["assignments"]) == 2
