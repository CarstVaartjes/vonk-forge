"""External model and credential routes: review, accept exactly the review, replay."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, insert, update
from sqlalchemy.orm import sessionmaker

from vonk_control.auth import Actor
from vonk_control.gateway.external_api import install_external_model_routes
from vonk_control.gateway.external_service import ExternalModelService
from vonk_control.gateway.litellm_surface import load_surface
from vonk_control.model_primitives import Base
from vonk_control.models.gateway import (
    GatewayCredentialBinding,
    GatewayExternalDeployment,
)
from vonk_control.operator_acceptance.resolution import (
    RECEIPT_HEADER,
    ControllerAPIRoute,
    OperatorReceiptResolver,
)

ROOT = Path(__file__).resolve().parents[2]
PARTNER = "https://api.partner.example"
ELSEWHERE = "https://elsewhere.example"


def _client(tmp_path: Path, role: str = "administrator") -> TestClient:
    return _client_and_engine(tmp_path, role)[0]


def _client_and_engine(
    tmp_path: Path, role: str = "administrator"
) -> tuple[TestClient, Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'external.sqlite'}")
    Base.metadata.create_all(engine)
    service = ExternalModelService(
        sessionmaker(engine, expire_on_commit=False),
        surface=lambda: load_surface(ROOT / "deploy/compose/litellm/surface.json"),
        clock=lambda: datetime(2026, 11, 26, tzinfo=UTC),
        credentials=lambda: frozenset({"PARTNER"}),
    )
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute
    install_external_model_routes(
        app, actor_dependency=Depends(lambda: Actor("test", role)), service=service
    )
    OperatorReceiptResolver(external_models=service).bind(app)
    return TestClient(app), engine


def _entry(**changes: object) -> dict[str, object]:
    params: dict[str, object] = {
        "model": "openai/partner-large",
        "api_base": f"{PARTNER}/v1",
        "api_key": "os.environ/VONK_CREDENTIAL_PARTNER",
        **changes,
    }
    return {"model_name": "partner-large", "litellm_params": params}


def _approve(client: TestClient, *origins: str) -> None:
    destinations = [{"origin": origin} for origin in origins]
    base = "/api/gateway/credentials/PARTNER"
    review = client.post(
        f"{base}/preview", json={"action": "put", "destinations": destinations}
    )
    assert review.status_code == 200, review.text
    accepted = client.post(
        f"{base}/accept",
        json={
            "request_id": str(uuid4()),
            "action": "put",
            "destinations": destinations,
            "effect_digest": review.json()["effect_digest"],
        },
    )
    assert accepted.status_code == 200, accepted.text


def _put(client: TestClient, entry: dict[str, object], revision: int | None, **kw):
    base = "/api/gateway/models/partner-large"
    request = {"action": "put", "entry": entry, "expected_revision": revision}
    review = client.post(f"{base}/preview", json=request)
    assert review.status_code == 200, review.text
    body = {
        "request_id": kw.get("request_id", str(uuid4())),
        **request,
        "effect_digest": kw.get("digest", review.json()["effect_digest"]),
    }
    sent = client.post(f"{base}/accept", json=body)
    return review.json(), sent


def test_a_model_is_accepted_exactly_as_reviewed_and_replays_by_request_id(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _approve(client, PARTNER)
    review, accepted = _put(client, _entry(), None)
    assert review["problems"] == []
    assert review["effects"] == [
        "gateway-restart",
        "open-key-access",
        "local-models-dark-on-failed-start",
    ]
    assert review["grace_seconds"] == 30
    # Three tries of one content, each waiting out the 165 s acknowledgement.
    assert review["failed_start_dark_seconds"] == 3 * 165
    assert accepted.status_code == 200, accepted.text
    assert RECEIPT_HEADER in accepted.headers
    request_id = accepted.json()["request_id"]
    original = {
        "request_id": request_id,
        "action": "put",
        "entry": _entry(),
        "expected_revision": None,
        "effect_digest": review["effect_digest"],
    }
    shown = client.get("/api/gateway/models/partner-large").json()
    assert shown["state"] == "pending"
    assert shown["credential_bindings"] == ["PARTNER"]
    assert "api_key" not in str(shown)

    # A lost response: the same request id reconnects to the original acceptance.
    again = client.post("/api/gateway/models/partner-large/accept", json=original)
    assert again.status_code == 200
    assert again.json()["revision"] == 1

    # A different change than the one reviewed is refused, not applied.
    _, refused = _put(client, _entry(rpm=5), 1, digest="0" * 64)
    assert refused.status_code == 409


def test_an_edit_keeps_its_own_alias_and_a_new_revision_returns_to_pending(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _approve(client, PARTNER)
    _put(client, _entry(), None)
    review, edited = _put(client, _entry(rpm=5), 1)
    # Break caught: the deployment's own alias was read as taken by another owner.
    assert [item["code"] for item in review["problems"]] == []
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2
    assert edited.json()["state"] == "pending"
    stale = client.post(
        "/api/gateway/models/partner-large/preview",
        json={"action": "put", "entry": _entry(rpm=6), "expected_revision": 1},
    )
    assert stale.status_code == 409  # read at revision 1, the model is at 2


def test_a_destination_the_binding_does_not_approve_is_refused_with_its_path(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _approve(client, PARTNER)
    review = client.post(
        "/api/gateway/models/validate",
        json={"entry": _entry(api_base=f"{ELSEWHERE}/v1")},
    ).json()
    assert [(item["path"], item["code"]) for item in review["problems"]] == [
        ("litellm_params.api_base", "destination_not_approved")
    ]


def test_a_binding_review_says_what_narrowing_withdraws_and_widening_does_not(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _approve(client, PARTNER)
    _put(client, _entry(), None)
    base = "/api/gateway/credentials/PARTNER/preview"
    wider = client.post(
        base,
        json={
            "action": "put",
            "destinations": [{"origin": PARTNER}, {"origin": ELSEWHERE}],
            "expected_revision": 1,
        },
    ).json()
    narrower = client.post(
        base,
        json={
            "action": "put",
            "destinations": [{"origin": ELSEWHERE}],
            "expected_revision": 1,
        },
    ).json()
    assert [item["withdrawn"] for item in wider["dependents"]] == [False]
    assert [item["withdrawn"] for item in narrower["dependents"]] == [True]
    assert narrower["credential_loaded"] is True


def test_only_an_administrator_reads_or_changes_external_models(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, role="viewer")
    assert client.get("/api/gateway/models").status_code == 403
    assert (
        client.post(
            "/api/gateway/models/validate", json={"entry": _entry()}
        ).status_code
        == 403
    )


def test_a_removal_that_is_already_complete_replays_as_accepted(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    _approve(client, PARTNER)
    _put(client, _entry(), None)
    request = {"action": "remove", "expected_revision": 1}
    review = client.post("/api/gateway/models/partner-large/preview", json=request)
    assert review.status_code == 200, review.text
    body = {
        **request,
        "request_id": str(uuid4()),
        "effect_digest": review.json()["effect_digest"],
    }
    first = client.post("/api/gateway/models/partner-large/accept", json=body)
    assert first.status_code == 200, first.text
    # The publication has not run, so the row still exists: the same id replays.
    # Delete it as the confirmed withdrawal does, then replay a lost response.
    with sessionmaker(engine).begin() as session:
        for row in session.query(GatewayExternalDeployment):
            session.delete(row)
    again = client.post("/api/gateway/models/partner-large/accept", json=body)
    assert again.status_code == 200, again.text
    assert again.json().get("revision") is None
    other = {**body, "request_id": str(uuid4())}
    assert (
        client.post("/api/gateway/models/partner-large/accept", json=other).status_code
        == 200
    )


def _binding_accept(client: TestClient, destinations, revision) -> int:
    request = {"action": "put", "destinations": destinations}
    review = client.post(
        "/api/gateway/credentials/PARTNER/preview",
        json={**request, "expected_revision": revision},
    )
    assert review.status_code == 200, review.text
    return client.post(
        "/api/gateway/credentials/PARTNER/accept",
        json={
            "request_id": str(uuid4()),
            **request,
            "expected_revision": revision,
            "effect_digest": review.json()["effect_digest"],
        },
    ).status_code


def test_a_binding_changed_by_another_accept_is_a_conflict_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Break caught: two accepts of one binding both wrote (the second overwrote the
    # first), or the loser met an unmapped database error.
    client = _client(tmp_path)
    write = ExternalModelService._write_binding
    table = GatewayCredentialBinding
    stamp = datetime(2026, 11, 26, tzinfo=UTC)

    def another_create(session, row, request, review, **kwargs):
        revision = write(session, row, request, review, **kwargs)
        session.execute(
            insert(table).values(
                name=review.name,
                version=1,
                revision=1,
                destinations=[],
                request_id="other",
                actor="other",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        return revision

    def another_update(session, row, request, review, **kwargs):
        # Another accept committed first: the row the ORM holds is one version behind.
        session.execute(
            update(table)
            .where(table.name == review.name)
            .values(version=table.version + 1)
            .execution_options(synchronize_session=False)
        )
        return write(session, row, request, review, **kwargs)

    monkeypatch.setattr(
        ExternalModelService, "_write_binding", staticmethod(another_create)
    )
    assert _binding_accept(client, [{"origin": PARTNER}], None) == 409
    monkeypatch.setattr(ExternalModelService, "_write_binding", staticmethod(write))
    _approve(client, PARTNER)
    monkeypatch.setattr(
        ExternalModelService, "_write_binding", staticmethod(another_update)
    )
    assert (
        _binding_accept(client, [{"origin": PARTNER}, {"origin": ELSEWHERE}], 1) == 409
    )
