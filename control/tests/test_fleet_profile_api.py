from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    AgentNode,
    Base,
    LocalRecipe,
    LocalRecipeRevision,
)
from vonk_control.recipe_contract import recipe_content_sha256, validate_recipe

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
PROFILE = "00000000-0000-4000-8000-000000000001"
REVISION = "00000000-0000-4000-8000-000000000002"
NODE = "spk_" + "1" * 32
FIXTURE = Path(__file__).parent / "fixtures" / "global" / "recipe-v1-minimal.json"


class Jobs:
    def list(self, **_kwargs):
        return []

    def get(self, _job_id):
        raise KeyError


def _setup() -> tuple[TestClient, object, MemoryAuditStore]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    document = json.loads(FIXTURE.read_text())
    validate_recipe(document)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE,
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
        session.add(
            LocalRecipe(
                id=PROFILE,
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
                id=REVISION,
                recipe_id=PROFILE,
                revision_number=1,
                lifecycle="resolved",
                schema_version=1,
                document=document,
                content_sha256=recipe_content_sha256(document),
                created_by="admin",
                created_at=NOW,
            )
        )
    codec = TokenCodec(b"p" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        fleet_profiles=FleetProfileService(sessions, clock=lambda: NOW),
    )

    def headers(role: str = "administrator") -> dict[str, str]:
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, audits


def _body() -> dict[str, object]:
    return {
        "name": "Studio ready",
        "description": "Keep one Spark ready for local chat.",
        "installation_policy": "keep-cached",
        "labels": {"purpose": "interactive"},
        "favorite": True,
        "assignments": [
            {
                "recipe_revision_id": REVISION,
                "topology_name": "solo",
                "desired_state": "running",
                "alias": "studio-chat",
                "nodes": [
                    {
                        "node_id": NODE,
                        "rank": 0,
                        "role": "entrypoint",
                        "endpoint_owner": True,
                    }
                ],
            }
        ],
    }


def test_profiles_are_authenticated_role_bound_and_audited() -> None:
    client, headers, audits = _setup()
    denied = client.post(
        "/api/v1/fleet-profiles",
        headers=headers("operator"),
        json=_body(),
    )
    created = client.post(
        "/api/v1/fleet-profiles",
        headers={**headers(), "x-request-id": "20000000-0000-4000-8000-000000000001"},
        json=_body(),
    )
    listed = client.get("/api/v1/fleet-profiles", headers=headers("viewer"))

    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["name"] == "Studio ready"
    assert listed.status_code == 200
    assert listed.json()["profiles"] == [created.json()]
    assert (
        audits.for_request("20000000-0000-4000-8000-000000000001").action
        == "fleet-profile.create"
    )


def test_profile_preview_exposes_blockers_and_apply_rejects_stale_plan() -> None:
    client, headers, _audits = _setup()
    created = client.post("/api/v1/fleet-profiles", headers=headers(), json=_body())
    profile_id = created.json()["id"]
    preview = client.post(
        f"/api/v1/fleet-profiles/{profile_id}/preview",
        headers=headers(),
        json={},
    )
    applied = client.post(
        f"/api/v1/fleet-profiles/{profile_id}/apply",
        headers=headers(),
        json={
            "plan_digest": "f" * 64,
            "request_key": "30000000-0000-4000-8000-000000000001",
        },
    )

    assert preview.status_code == 200
    assert preview.json()["allowed"] is False
    assert preview.json()["reasons"][0]["code"] == "profile.build_missing"
    assert applied.status_code == 409
    assert "blocked" in applied.json()["detail"]


def test_profile_routes_have_stable_openapi_operation_ids() -> None:
    client, headers, _audits = _setup()
    document = client.get("/openapi.json", headers=headers()).json()

    assert (
        document["paths"]["/api/v1/fleet-profiles"]["get"]["operationId"]
        == "listFleetProfiles"
    )
    assert (
        document["paths"]["/api/v1/fleet-profiles/{profile_id}/apply"]["post"][
            "operationId"
        ]
        == "applyFleetProfile"
    )
