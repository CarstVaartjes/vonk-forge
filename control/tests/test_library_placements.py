from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileAssignmentPreview,
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileScopePreview,
)
from vonk_control.library_placement_contract import (
    LibraryPlacementApplyRequest,
    LibraryPlacementPreviewRequest,
)
from vonk_control.library_placements import (
    LibraryPlacementConflict,
    LibraryPlacementService,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
RECIPE = "00000000-0000-4000-8000-000000000001"
REVISION = "00000000-0000-4000-8000-000000000002"
PROFILE = "00000000-0000-4000-8000-000000000003"
APPLICATION = "00000000-0000-4000-8000-000000000004"
NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32


def node(node_id: str, rank: int, role: str, *, endpoint_owner: bool):
    return SimpleNamespace(
        node_id=node_id,
        rank=rank,
        role=role,
        endpoint_owner=endpoint_owner,
        disk_free_bytes=1_000,
        disk_required_bytes=100,
        disk_free_after_bytes=900,
        memory_available_bytes=800,
        memory_required_bytes=200,
        memory_free_after_bytes=600,
    )


def group(
    nodes: list[SimpleNamespace],
    *,
    eligible: bool = True,
    reasons: list[SimpleNamespace] | None = None,
):
    return SimpleNamespace(
        node_ids=[item.node_id for item in nodes],
        nodes=nodes,
        eligible=eligible,
        reasons=reasons or [],
        installation_ids=[],
        run_ids=[],
        install_state="not_present",
        load_state="not_loaded",
    )


class Projection:
    def __init__(self, selected_group, *, node_count: int) -> None:
        self.selected_group = selected_group
        self.node_count = node_count

    def detail(self, recipe_id: str):
        if recipe_id != RECIPE:
            raise KeyError(recipe_id)
        return SimpleNamespace(
            generated_at=NOW,
            recipe=SimpleNamespace(recipe_id=RECIPE, title="Tiny model"),
            selected_revision=SimpleNamespace(id=REVISION, lifecycle="resolved"),
            topology=SimpleNamespace(
                name="pair" if self.node_count == 2 else "solo",
                node_count=self.node_count,
            ),
            placement=[
                SimpleNamespace(
                    recommendations=[self.selected_group]
                    if self.selected_group.eligible
                    else [],
                    rejected_groups=[]
                    if self.selected_group.eligible
                    else [self.selected_group],
                )
            ],
        )


class Profiles:
    def __init__(self) -> None:
        self.apply_calls: list[dict[str, object]] = []
        self.application_row: FleetProfileApplicationView | None = None

    def ensure_internal_placement(self, _profile_id, _assignment, *, actor):
        assert actor == "admin"

    def preview(self, profile_id: str) -> FleetProfilePreview:
        return FleetProfilePreview(
            profile_id=profile_id,
            profile_name="internal",
            profile_digest="a" * 64,
            generated_at=NOW,
            allowed=True,
            scope=FleetProfileScopePreview(node_ids=[NODE_A]),
            summary=FleetProfilePlanSummary(
                already_correct=0,
                placements=1,
                builds=1,
                distributions=1,
                installs=1,
                starts=0,
                stops=0,
                uninstalls=0,
                blockers=0,
            ),
            assignments=[
                FleetProfileAssignmentPreview(
                    assignment_id="00000000-0000-4000-8000-000000000005",
                    recipe_revision_id=REVISION,
                    recipe_title="Tiny model",
                    desired_state="installed",
                    current_state="not-placed",
                    node_ids=[NODE_A],
                    actions=[
                        "create-placement",
                        "build",
                        "distribute-image",
                        "install",
                    ],
                    reasons=[],
                )
            ],
            steps=[
                FleetProfilePlanStep(
                    index=0,
                    kind="create-placement",
                    assignment_id="00000000-0000-4000-8000-000000000005",
                    recipe_revision_id=REVISION,
                    node_ids=[NODE_A],
                    label="Place Tiny model",
                ),
                FleetProfilePlanStep(
                    index=1,
                    kind="build",
                    assignment_id="00000000-0000-4000-8000-000000000005",
                    recipe_revision_id=REVISION,
                    node_ids=[NODE_A],
                    label="Build Tiny model",
                ),
            ],
            reasons=[],
            plan_digest="b" * 64,
        )

    def apply_internal_placement(self, profile_id: str, **kwargs):
        self.apply_calls.append({"profile_id": profile_id, **kwargs})
        metadata = {
            **kwargs["metadata"],
            "plan_digest": kwargs["placement_plan_digest"],
        }
        self.application_row = FleetProfileApplicationView(
            id=APPLICATION,
            profile_id=profile_id,
            profile_digest="a" * 64,
            plan_digest="b" * 64,
            state="queued",
            current_step=0,
            total_steps=2,
            current_operation_id=None,
            status_reason=None,
            progress={
                "completed_steps": 0,
                "total_steps": 2,
                "library_placement": metadata,
            },
            result=None,
            created_at=NOW,
            updated_at=NOW,
        )
        return self.application_row

    def replay_internal_placement(self, request_key: str):
        if self.application_row is None:
            return None
        return (
            self.application_row
            if self.apply_calls[0]["request_key"] == request_key
            else None
        )

    def application(self, application_id: str):
        if self.application_row is None or application_id != APPLICATION:
            raise KeyError(application_id)
        return self.application_row


def request(*, invocation: str = "button", nodes: list[str] | None = None):
    return LibraryPlacementPreviewRequest(
        recipe_id=RECIPE,
        node_ids=nodes or [NODE_A],
        desired_state="installed",
        invocation=invocation,
    )


def test_pointer_and_keyboard_use_identical_authority_and_selected_identity() -> None:
    profiles = Profiles()
    service = LibraryPlacementService(
        Projection(
            group([node(NODE_A, 0, "entrypoint", endpoint_owner=True)]), node_count=1
        ),
        profiles,
    )

    pointer = service.preview(request(invocation="drag-drop"), actor="admin")
    keyboard = service.preview(request(invocation="keyboard"), actor="admin")

    assert pointer.plan_digest == keyboard.plan_digest
    assert pointer.selected_node_ids == [NODE_A]
    assert pointer.selected_nodes[0].role == "entrypoint"
    assert pointer.allowed is True
    assert [step.kind for step in pointer.steps] == ["create-placement", "build"]


def test_dual_spark_preview_preserves_ranked_group_and_capacity_blockers() -> None:
    rejected = group(
        [
            node(NODE_A, 0, "entrypoint", endpoint_owner=True),
            node(NODE_B, 1, "worker", endpoint_owner=False),
        ],
        eligible=False,
        reasons=[
            SimpleNamespace(
                code="install.insufficient_disk",
                detail="Spark B lacks the required disk headroom.",
                severity="error",
            )
        ],
    )
    service = LibraryPlacementService(Projection(rejected, node_count=2), Profiles())

    preview = service.preview(request(nodes=[NODE_A, NODE_B]), actor="admin")

    assert preview.allowed is False
    assert [item.node_id for item in preview.selected_nodes] == [NODE_A, NODE_B]
    assert [item.rank for item in preview.selected_nodes] == [0, 1]
    assert [item.code for item in preview.blockers] == ["install.insufficient_disk"]


def test_apply_is_digest_bound_and_exposes_durable_progress_and_locations() -> None:
    profiles = Profiles()
    projection = Projection(
        group([node(NODE_A, 0, "entrypoint", endpoint_owner=True)]), node_count=1
    )
    service = LibraryPlacementService(projection, profiles)
    preview = service.preview(request(), actor="admin")
    apply = LibraryPlacementApplyRequest(
        **request().model_dump(),
        plan_digest=preview.plan_digest,
        request_key="00000000-0000-4000-8000-000000000006",
    )

    result = service.apply(apply, actor="admin")
    replayed = service.apply(apply, actor="admin")

    assert result.state == replayed.state == "queued"
    assert result.total_steps == 2
    assert result.selected_node_ids == [NODE_A]
    assert profiles.apply_calls[0]["placement_plan_digest"] == preview.plan_digest
    assert len(profiles.apply_calls) == 1

    stale = apply.model_copy(
        update={
            "plan_digest": "0" * 64,
            "request_key": "00000000-0000-4000-8000-000000000007",
        }
    )
    with pytest.raises(LibraryPlacementConflict, match="stale"):
        service.apply(stale, actor="admin")
