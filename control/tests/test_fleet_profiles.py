from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileConflict, FleetProfileService
from vonk_control.models import (
    AgentNode,
    Base,
    FleetProfile,
    FleetProfileApplication,
    LocalRecipe,
    LocalRecipeRevision,
    RecipeBuild,
)
from vonk_control.recipe_contract import recipe_content_sha256, validate_recipe

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "global" / "recipe-v1-minimal.json"


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"


def _node_id(value: int) -> str:
    return "spk_" + f"{value:032x}"


def _database() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _recipe_document() -> dict[str, object]:
    document = json.loads(FIXTURE.read_text())
    validate_recipe(document)
    return document


def _seed(sessions: sessionmaker[Session]) -> tuple[str, str]:
    recipe_id = _uuid(1)
    revision_id = _uuid(2)
    document = _recipe_document()
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(1),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
        session.add(
            LocalRecipe(
                id=recipe_id,
                slug="tiny-chat",
                title="Tiny Chat",
                description="A small illustrative chat recipe.",
                source_kind="local",
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            LocalRecipeRevision(
                id=revision_id,
                recipe_id=recipe_id,
                revision_number=1,
                lifecycle="resolved",
                schema_version=1,
                document=document,
                content_sha256=recipe_content_sha256(document),
                created_by="admin",
                created_at=NOW,
            )
        )
    return recipe_id, revision_id


def _input(revision_id: str, *, name: str = "Studio ready") -> FleetProfileInput:
    return FleetProfileInput.model_validate(
        {
            "name": name,
            "description": "Keep the studio Spark ready for local chat.",
            "installation_policy": "keep-cached",
            "labels": {"purpose": "interactive"},
            "favorite": True,
            "assignments": [
                {
                    "recipe_revision_id": revision_id,
                    "topology_name": "solo",
                    "desired_state": "running",
                    "alias": "studio-chat",
                    "nodes": [
                        {
                            "node_id": _node_id(1),
                            "rank": 0,
                            "role": "entrypoint",
                            "endpoint_owner": True,
                        }
                    ],
                }
            ],
        }
    )


def test_profile_contract_rejects_ambiguous_or_incomplete_assignments() -> None:
    value = _input(_uuid(2)).model_dump(mode="json")
    value["assignments"][0]["alias"] = None
    with pytest.raises(ValidationError, match="require an endpoint alias"):
        FleetProfileInput.model_validate(value)

    value = _input(_uuid(2)).model_dump(mode="json")
    value["assignments"][0]["nodes"][0]["endpoint_owner"] = False
    with pytest.raises(ValidationError, match="exactly one endpoint owner"):
        FleetProfileInput.model_validate(value)


def test_profile_create_is_server_owned_validated_and_digest_stable() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)

    created = service.create(_input(revision_id), actor="admin")
    loaded = service.get(created.id)
    listed = service.list()

    assert created == loaded
    assert listed.profiles == [created]
    assert created.name == "Studio ready"
    assert created.assignments[0].recipe_title == "Tiny Chat"
    assert created.assignments[0].nodes[0].node_id == _node_id(1)
    assert len(created.profile_digest) == 64
    assert created.profile_digest == loaded.profile_digest


def test_direct_placement_profile_is_deterministic_and_hidden_from_saved_profiles() -> (
    None
):
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile_id = _uuid(30)
    assignment = _input(revision_id).assignments[0]

    first = service.ensure_internal_placement(profile_id, assignment, actor="admin")
    replay = service.ensure_internal_placement(profile_id, assignment, actor="admin")

    assert first == replay
    assert first.id == profile_id
    assert service.list().profiles == []


def test_profile_delete_removes_terminal_application_history() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")
    application_id = _uuid(20)
    with sessions.begin() as session:
        session.add(
            FleetProfileApplication(
                id=application_id,
                request_key=_uuid(21),
                profile_id=profile.id,
                profile_digest=profile.profile_digest,
                plan_digest="e" * 64,
                state="waiting-for-operator",
                plan={"steps": []},
                current_step=0,
                current_operation_id=None,
                progress={},
                result=None,
                status_reason="Operator review required",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    service.delete(profile.id)

    with sessions() as session:
        assert session.get(FleetProfile, profile.id) is None
        assert session.get(FleetProfileApplication, application_id) is None


def test_profile_validation_rejects_unknown_sparks_and_recipe_topology_drift() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["nodes"][0]["node_id"] = _node_id(9)

    with pytest.raises(FleetProfileConflict, match="active enrolled Fleet member"):
        service.create(FleetProfileInput.model_validate(value), actor="admin")

    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["topology_name"] = "pair"
    with pytest.raises(FleetProfileConflict, match="topology"):
        service.create(FleetProfileInput.model_validate(value), actor="admin")


def test_profile_validation_rejects_rank_order_that_mapping_would_rewrite() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    document = _recipe_document()
    topology = document["topology"]
    leader = deepcopy(topology["roles"][0])
    leader.update({"name": "leader", "count": 1, "endpoint_owner": True})
    worker = deepcopy(leader)
    worker.update({"name": "worker", "endpoint_owner": False})
    topology.update(
        {
            "name": "pair",
            "mode": "distributed",
            "node_count": 2,
            "roles": [leader, worker],
            "parallelism": {
                "world_size": 2,
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            "start_order": ["worker", "leader"],
            "stop_order": ["leader", "worker"],
        }
    )
    document["artifacts"][0]["roles"] = ["leader", "worker"]
    validate_recipe(document)
    with sessions.begin() as session:
        session.execute(
            LocalRecipeRevision.__table__.update()
            .where(LocalRecipeRevision.id == revision_id)
            .values(
                document=document,
                content_sha256=recipe_content_sha256(document),
            )
        )
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )

    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0].update(
        {
            "topology_name": "pair",
            "nodes": [
                {
                    "node_id": _node_id(2),
                    "rank": 0,
                    "role": "leader",
                    "endpoint_owner": True,
                },
                {
                    "node_id": _node_id(1),
                    "rank": 1,
                    "role": "worker",
                    "endpoint_owner": False,
                },
            ],
        }
    )

    with pytest.raises(
        FleetProfileConflict, match="deterministic Spark identity order"
    ):
        FleetProfileService(sessions, clock=lambda: NOW).create(
            FleetProfileInput.model_validate(value), actor="admin"
        )


def test_profile_preview_explains_prerequisites_then_builds_one_atomic_plan() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")

    build_plan = service.preview(profile.id)
    assert build_plan.allowed is True
    assert build_plan.summary.blockers == 0
    assert build_plan.summary.builds == 1
    assert build_plan.assignments[0].current_state == "not-placed"
    assert build_plan.assignments[0].actions == [
        "create-placement",
        "build",
        "distribute-image",
        "install",
        "start",
    ]

    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=_uuid(3),
                recipe_revision_id=revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="a" * 64,
                build_input_sha256="b" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=f"sha256:{'c' * 64}",
                oci_layout_sha256="d" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert preview.summary.model_dump() == {
        "already_correct": 0,
        "placements": 1,
        "builds": 0,
        "distributions": 1,
        "installs": 1,
        "starts": 1,
        "stops": 0,
        "uninstalls": 0,
        "blockers": 0,
    }
    assert [step.kind for step in preview.steps] == [
        "create-placement",
        "distribute-image",
        "install",
        "start",
    ]
    assert len(preview.plan_digest) == 64

    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(4),
        actor="admin",
    )
    replay = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(4),
        actor="admin",
    )
    assert application == replay
    assert application.state == "queued"
    assert application.total_steps == 4


def test_profile_apply_rejects_a_stale_preview_and_request_key_reuse() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")

    with pytest.raises(FleetProfileConflict, match="stale"):
        service.apply(
            profile.id, plan_digest="f" * 64, request_key=_uuid(5), actor="admin"
        )

    updated = service.update(
        profile.id, _input(revision_id, name="Studio exact"), actor="admin"
    )
    assert updated.profile_digest != profile.profile_digest


def test_profile_application_resumes_from_persisted_step_context() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=_uuid(6),
                recipe_revision_id=revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="a" * 64,
                build_input_sha256="b" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=f"sha256:{'c' * 64}",
                oci_layout_sha256="d" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(7),
        actor="admin",
    )

    class Operations:
        def preview_mapping(self, *_args, **_kwargs):
            return SimpleNamespace(generation=3)

        def create_mapping(self, *_args, **_kwargs):
            return _uuid(8)

        def preview_image_distribution(self, *_args, **_kwargs):
            return SimpleNamespace(node_ids=())

    restarted = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        recipe_operations=Operations(),
    )

    assert restarted.tick() is True
    after_mapping = restarted.application(application.id)
    assert after_mapping.state == "running"
    assert after_mapping.current_step == 1

    assert restarted.tick() is True
    after_distribution = restarted.application(application.id)
    assert after_distribution.current_step == 2
    assert after_distribution.current_operation_id is None
