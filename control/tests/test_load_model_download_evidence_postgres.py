"""A load's model download is decided by the cache's current evidence.

A model the NAS lacks is a dependency the load satisfies itself: its
model-download phase requests the download the way an operator's or the
sweep's prefetch does, waits on it, and completes when it lands. A model that
another download brought after the load was planned completes the phase with
nothing fetched: the plan's missing byte count is planning-time evidence.

The real cache, catalog and phase executor over PostgreSQL; the model's
upstream answers in memory.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import Engine, select
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.distribution_executor import CompositeDistributionPhaseExecutor
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import CatalogDocumentRevision, ModelCacheSet, RecipeBuild
from vonk_control.run_switch_contract import (
    ArtifactStorageImpact,
    RunSwitchModelDownloadResult,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
)
from vonk_control.run_switch_operations.artifact_validation import (
    _validate_artifact_execution,
)
from vonk_forge_contracts import document_sha256

from .test_recipe_operations import setup_services

WEIGHTS = bytes(range(256)) * 4
SELECTOR = "vonk-forge/synthetic-tiny-fp16"


class _Upstream:
    """huggingface.co for the one model file; what it served."""

    def __init__(self) -> None:
        self.served_bytes = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if not request.url.path.endswith("/model.safetensors"):
            return httpx2.Response(404, request=request)
        start = 0
        if requested := request.headers.get("range"):
            start = int(requested.removeprefix("bytes=").split("-", 1)[0])
        body = WEIGHTS[start:]
        self.served_bytes += len(body)
        return httpx2.Response(
            206 if requested else 200,
            request=request,
            content=body,
            headers={"content-length": str(len(body))},
        )


class _Nas:
    """The catalog, the NAS cache and the load's model-download phase."""

    def __init__(self, tmp_path: Path, engine: Engine) -> None:
        model = json.loads(
            resources.files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text()
        )
        model["source"]["repository"] = "https://huggingface.co/vonk-forge/tiny"
        (weights,) = model["files"]
        weights["sha256"] = hashlib.sha256(WEIGHTS).hexdigest()
        weights["size_bytes"] = len(WEIGHTS)
        self.model_content_sha256 = document_sha256(model)

        def bind_model(recipe: dict[str, Any]) -> None:
            recipe["models"][0]["model"]["content_sha256"] = self.model_content_sha256

        sessions, _lifecycle, _queue, _mapping, build_id, _nodes = setup_services(
            tmp_path,
            engine=engine,
            model_transform=lambda document: document.update(model),
            recipe_transform=bind_model,
        )
        self.sessions = sessions
        with sessions() as session:
            build = session.get(RecipeBuild, build_id)
            assert build is not None
            self.recipe_revision_id = build.recipe_revision_id
            model_revision_id = session.scalar(
                select(CatalogDocumentRevision.id).where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.content_digest == self.model_content_sha256,
                )
            )
            assert model_revision_id is not None
        self.now = datetime(2026, 10, 13, tzinfo=UTC)
        CatalogEntityService(sessions, clock=lambda: self.now).resolve(
            model_revision_id, actor="operator"
        )
        self.upstream = _Upstream()
        self.cache = ModelCacheService(
            sessions,
            tmp_path / "nas-cache",
            reserve_bytes=0,
            clock=lambda: self.now,
            http_client=httpx2.Client(
                transport=httpx2.MockTransport(self.upstream.handle),
                follow_redirects=False,
            ),
        )
        self.executor = CompositeDistributionPhaseExecutor(
            sessions, None, None, model_cache=self.cache, clock=lambda: self.now
        )

    def close(self) -> None:
        self.executor.close()
        self.cache.close()

    def plan_while_missing(self) -> RunSwitchPlan:
        """What a load planned while the NAS lacked the model carries."""

        manifest = self.cache.resolve_artifact_set(
            model_content_sha256=self.model_content_sha256,
            recipe_revision_id=self.recipe_revision_id,
        )
        return RunSwitchPlan.model_construct(
            None,
            model_content_sha256=self.model_content_sha256,
            recipe_revision_id=self.recipe_revision_id,
            preparation=None,
            storage=ArtifactStorageImpact.model_construct(
                None,
                artifact_set_sha256=manifest.digest,
                artifact_set_bytes=manifest.expected_bytes,
                artifact_digests=[item.sha256 for item in manifest.artifacts],
                missing_nas_bytes=manifest.expected_bytes,
            ),
        )

    def download_phase(self, plan: RunSwitchPlan) -> str:
        """The load's model-download phase: the cache operation it waits on."""

        started = self.executor.execute(
            plan,
            _PHASE,
            item_index=0,
            actor="admin",
            request_key=str(uuid.uuid4()),
            progress=RunSwitchOperationResult(),
        )
        assert started.operation_id is not None
        return started.operation_id

    def settle(self, operation_id: str) -> RunSwitchModelDownloadResult:
        for _ in range(4):
            child = self.executor.get(operation_id)
            assert child is not None
            if child.state == "succeeded":
                assert isinstance(child.result, RunSwitchModelDownloadResult)
                return child.result
            assert child.state in {"queued", "running"}, child
            self.cache.run_pending(limit=4)
        raise AssertionError(f"the download did not land: {child}")

    def cached(self, artifact_set_sha256: str) -> bool:
        with self.sessions() as session:
            row = session.get(ModelCacheSet, artifact_set_sha256)
            return row is not None and row.state == "cached"


_PHASE = RunSwitchPhase.model_construct(
    None, index=1, kind="transfer", subphase="model-download"
)


@pytest.fixture
def nas(tmp_path: Path, postgres_engine: Engine):
    world = _Nas(tmp_path, postgres_engine)
    try:
        yield world
    finally:
        world.close()


def test_a_load_downloads_the_model_the_nas_lacks(nas: _Nas) -> None:
    plan = nas.plan_while_missing()
    wanted = plan.storage.artifact_set_sha256
    assert wanted is not None and not nas.cached(wanted)

    operation_id = nas.download_phase(plan)
    child = nas.executor.get(operation_id)
    assert child is not None and child.state in {"queued", "running"}  # it waits

    landed = nas.settle(operation_id)
    _validate_artifact_execution(plan, _PHASE, landed)
    assert landed.artifact_set_sha256 == wanted
    assert nas.cached(wanted)
    assert nas.upstream.served_bytes == len(WEIGHTS)


@pytest.mark.parametrize("arrives", ["before the phase", "while the phase waits"])
def test_a_model_another_download_brought_completes_the_load(
    nas: _Nas, arrives: str
) -> None:
    plan = nas.plan_while_missing()
    operator = nas.cache.download_model_selector(
        SELECTOR, actor="admin", request_key=str(uuid.uuid4())
    )
    if arrives == "before the phase":
        nas.settle(operator.id)
    operation_id = nas.download_phase(plan)

    landed = nas.settle(operation_id)
    _validate_artifact_execution(plan, _PHASE, landed)
    assert nas.cached(landed.artifact_set_sha256)
    # The model came from upstream once, whoever asked first.
    assert nas.upstream.served_bytes == len(WEIGHTS)

    # A fresh load of the same model finds it cached and fetches nothing.
    again = nas.plan_while_missing()
    fresh = nas.settle(nas.download_phase(again))
    _validate_artifact_execution(again, _PHASE, fresh)
    assert fresh.downloaded_bytes == 0
