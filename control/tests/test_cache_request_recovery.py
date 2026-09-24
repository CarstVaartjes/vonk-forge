"""Accepted cache intent survives duplicate admissions and response recovery."""

from __future__ import annotations

import http.client
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.auth import Actor
from vonk_control.model_cache import (
    CacheOperationView,
    ModelCacheConflict,
    ModelCacheService,
)
from vonk_control.models import Base, Job, ModelCacheOperation
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
    RecipeImageAvailabilityView,
)
from vonk_control.recipe_image_availability_api import install_recipe_operator_routes
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from cluster_profiles.cli import main
from cluster_profiles.control_client import ControlClient

from .test_model_cache import _artifact
from .test_recipe_image_availability import (
    Transport,
    _add_head,
    _add_revision,
    _recipe,
    _runtime,
)


def _collide_inserts(
    engine: Engine,
    table: str,
    request_field: str,
    key: str,
    calls: tuple[Callable[[], object], Callable[[], object]],
) -> list[object]:
    """Hold both real inserts after their absence checks, not a mocked lock."""

    barrier = threading.Barrier(2, timeout=10)
    arrivals: list[int] = []

    def before_insert(_conn, _cursor, statement, parameters, _context, _many):
        if (
            statement.startswith(f"INSERT INTO {table} ")
            and isinstance(parameters, Mapping)
            and parameters.get(request_field) == key
        ):
            arrivals.append(threading.get_ident())
            barrier.wait()

    def capture(call: Callable[[], object]) -> object:
        try:
            return call()
        except (ModelCacheConflict, RecipeImageAvailabilityError) as error:
            return error

    event.listen(engine, "before_cursor_execute", before_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(capture, call) for call in calls]
            results = [future.result(timeout=20) for future in futures]
    finally:
        event.remove(engine, "before_cursor_execute", before_insert)
    assert len(arrivals) == 2
    return results


def _start_together(
    calls: tuple[Callable[[], object], Callable[[], object]],
) -> list[tuple[int, object]]:
    """Start two real PostgreSQL submissions together without pinning their order."""

    barrier = threading.Barrier(2, timeout=10)

    def capture(index: int, call: Callable[[], object]) -> tuple[int, object]:
        barrier.wait()
        try:
            return index, call()
        except (ModelCacheConflict, RecipeImageAvailabilityError) as error:
            return index, error

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(capture, index, call) for index, call in enumerate(calls)
        ]
        return [future.result(timeout=20) for future in futures]


@pytest.mark.parametrize("different_issuer", [False, True])
def test_model_duplicate_insert_adopts_only_identical_issuer_intent(
    postgres_engine: Engine, tmp_path: Path, different_issuer: bool
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    services = [
        ModelCacheService(
            sessions, tmp_path / "models", reserve_bytes=0, fixture_sources=True
        )
        for _ in range(2)
    ]
    artifact = _artifact(tmp_path, b"concurrent request", model_content_sha256="a" * 64)
    manifest = services[0].resolve_artifact_set(
        model_content_sha256="a" * 64, artifacts=[artifact]
    )
    # Existing identity/membership is not a completed artifact. This isolates
    # the operation-key race from the independent set-creation constraint.
    with sessions.begin() as session:
        services[0]._ensure_set(session, manifest)
    preview = services[0].download_preview(artifact_set_sha256=manifest.digest)
    key = str(uuid.uuid4())

    def submit(index: int):
        return services[index].start_download(
            actor="second" if different_issuer and index else "first",
            request_key=key,
            selector="selected-model",
            artifact_set_sha256=manifest.digest,
            plan_digest=str(preview["plan_digest"]),
            force=True,
        )

    # Model-set/object reference gates serialize same-target requests before
    # the operation INSERT. A barrier immediately before that INSERT would
    # deadlock the legitimate winner against a contender that must back off.
    # Start both requests concurrently, then exercise the same-key replay after
    # any gate-busy response has unwound.
    results = _start_together((lambda: submit(0), lambda: submit(1)))
    conflicts = [
        result for _index, result in results if isinstance(result, ModelCacheConflict)
    ]
    accepted = [
        result for _index, result in results if isinstance(result, CacheOperationView)
    ]
    assert accepted
    assert len(conflicts) + len(accepted) == 2
    assert all(
        error.code
        in (
            {"artifact.reference_busy", "model_cache.request_key_reused"}
            if different_issuer
            else {"artifact.reference_busy"}
        )
        for error in conflicts
    )
    if different_issuer:
        assert len(accepted) == 1
        with sessions() as session:
            stored = session.scalar(select(ModelCacheOperation))
            assert stored is not None
            winning_actor = stored.actor
            assert stored.request_key == key
            assert stored.artifact_set_sha256 == manifest.digest
            assert stored.plan_digest == preview["plan_digest"]
            assert stored.payload["selector"] == "selected-model"
            assert stored.payload["force_refresh"] is True
        winning_index = 0 if winning_actor == "first" else 1
        assert [
            index for index, result in results if isinstance(result, CacheOperationView)
        ] == [winning_index]
        losing_actor = "second" if winning_actor == "first" else "first"
        with pytest.raises(ModelCacheConflict) as reused:
            services[0].start_download(
                actor=losing_actor,
                request_key=key,
                selector="selected-model",
                artifact_set_sha256=manifest.digest,
                plan_digest=str(preview["plan_digest"]),
                force=True,
            )
        assert reused.value.code == "model_cache.request_key_reused"
    else:
        replay = submit(1)
        assert isinstance(replay, CacheOperationView)
        accepted.append(replay)
    assert len({result.id for result in accepted}) == 1
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(ModelCacheOperation)) == 1
        )


@pytest.mark.parametrize("different_intent", [False, True])
def test_recipe_duplicate_insert_adopts_only_identical_original_intent(
    postgres_engine: Engine, tmp_path: Path, different_intent: bool
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    recipe = _recipe("recipe-image.json")
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "race-recipe", recipe))
    services = [
        RecipeImageAvailabilityService(
            sessions,
            storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
            authority=lambda *_args, **_kwargs: (recipe, _runtime()),
            transport=Transport(),
            clock=lambda: datetime.now(UTC),
        )
        for _ in range(2)
    ]
    key = str(uuid.uuid4())

    def submit(index: int):
        return services[index].start_selector(
            recipe.identity.slug,
            actor="operator",
            request_id=key,
            force=bool(index) if different_intent else True,
        )

    results = _collide_inserts(
        postgres_engine,
        "jobs",
        "request_id",
        key,
        (lambda: submit(0), lambda: submit(1)),
    )
    conflicts = [
        result for result in results if isinstance(result, RecipeImageAvailabilityError)
    ]
    assert len(conflicts) == int(different_intent)
    assert all(error.code == "recipe_image.request_key_reused" for error in conflicts)
    accepted = [
        result for result in results if isinstance(result, RecipeImageAvailabilityView)
    ]
    assert len(accepted) + len(conflicts) == 2
    assert len({result.id for result in accepted}) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.parametrize(
    "delivery", ["normal", "lost_headers", "lost_body", "malformed_body"]
)
def test_recipe_lookup_preserves_id_visibility_and_private_request_correlation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], delivery: str
) -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-image.json")
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "lookup-recipe", recipe))
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    actor = Actor("issuer", "administrator")
    app = FastAPI()
    install_recipe_operator_routes(
        app, actor_dependency=Depends(lambda: actor), service=service
    )
    with TestClient(app) as client:
        key = str(uuid.uuid4())
        path = f"/api/recipe/{recipe.identity.slug}/download"
        body = {"request_key": key}
        assert client.post(path, json=body | {"with_model": True}).status_code == 422
        accepted = client.post(path, json=body)
        assert accepted.status_code == 202, accepted.text
        receipt = accepted.json()
        assert client.get(f"/api/recipe/requests/{key}").json() == receipt
        assert client.post(path, json=body).json() == receipt
        assert client.post("/api/recipe/absent/download", json=body).status_code == 409

        # Exercise the standalone CLI's generated request/response contracts
        # against registered routes and the real operation/storage owner.
        token = tmp_path / "cli-token"
        token.write_text("fixture-token")
        token.chmod(0o600)
        followed_paths: list[str] = []
        delivered_key = str(uuid.uuid4())
        delivered_receipt: dict[str, object] = {}

        class OpenedResponse(BytesIO):
            def __init__(self, response, *, failure=None):
                super().__init__(response.content)
                self.failure = failure
                self.status = response.status_code
                self.headers = Message()
                for name, value in response.headers.items():
                    self.headers[name] = value

            def read(self, size=-1):
                if self.failure == "lost_body":
                    raise http.client.IncompleteRead(b'{"id":', 100)
                if self.failure == "malformed_body":
                    return b'{"id":'
                return super().read(size)

            def __exit__(self, *_args: object) -> None:
                self.close()

        def opener(request: Request, timeout: float):
            followed_paths.append(request.selector)
            if request.selector.startswith("/api/recipe/operations/"):
                service.run_pending()
            data = request.data
            assert data is None or isinstance(data, bytes)
            response = client.request(
                request.get_method(),
                request.selector,
                content=data,
                headers=dict(request.header_items()),
            )
            if request.get_method() == "POST":
                # The real owner has committed the new request before delivery
                # fails. The CLI must recover it without another POST.
                assert not delivered_receipt
                delivered_receipt.update(response.json())
                if delivery == "lost_headers":
                    raise URLError(ConnectionResetError())
                return OpenedResponse(response, failure=delivery)
            return OpenedResponse(response)

        control = ControlClient("https://forge.example.test", token, opener=opener)
        assert (
            main(
                ("recipe", "progress", "--request-key", key, "--json"),
                control_client=control,
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out) == receipt
        service.run_pending()  # Finish the earlier visibility-test operation.
        assert (
            main(
                (
                    "recipe",
                    "download",
                    recipe.identity.slug,
                    "--request-key",
                    delivered_key,
                    "--json",
                    "--interval-seconds",
                    "0.01",
                ),
                control_client=control,
            )
            == 0
        )
        completed = json.loads(capsys.readouterr().out)
        assert completed["id"] == delivered_receipt["id"] != receipt["id"]
        assert completed["request_id"] == delivered_key
        assert completed["state"] == "succeeded"
        assert followed_paths == [
            f"/api/recipe/requests/{key}",
            path,
            *(
                [f"/api/recipe/requests/{delivered_key}"]
                if delivery != "normal"
                else []
            ),
            f"/api/recipe/operations/{completed['id']}",
        ]
        actor = Actor("another-issuer", "administrator")
        missing = client.get(f"/api/recipe/requests/{uuid.uuid4()}")
        hidden = client.get(f"/api/recipe/requests/{key}")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()
        assert client.get(f"/api/recipe/operations/{receipt['id']}").status_code == 200
        assert client.post(path, json=body).status_code == 409
        actor = Actor("issuer", "viewer")
        assert client.get(f"/api/recipe/requests/{key}").status_code == 200
        assert client.post(path, json=body).status_code == 403
