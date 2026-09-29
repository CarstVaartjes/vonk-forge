from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, Job
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256


def _drain(cache: ModelCacheService, operation_id: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        cache.tick()
        operation = cache.get_operation(operation_id)
        if operation.state in {"succeeded", "failed"}:
            assert operation.state == "succeeded", operation.failure
            return
        time.sleep(0.01)
    raise AssertionError("model cache operation did not settle")


def test_missing_managed_model_object_is_redownloaded_without_rebuilding_image(
    tmp_path: Path,
) -> None:
    payloads = {
        "/models/synthetic-tiny/resolve/" + "0" * 40 + "/weights-a.bin": b"weights-a",
        "/models/synthetic-tiny/resolve/" + "0" * 40 + "/weights-b.bin": b"weights-b",
    }
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        return httpx2.Response(200, request=request, content=payloads[request.url.path])

    model_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model_raw["source"] = {
        "repository": "https://huggingface.co/models/synthetic-tiny",
        "revision": "0" * 40,
    }
    model_raw["files"] = [
        {
            "id": key,
            "path": f"{key}.bin",
            "roles": ["weights"],
            "sha256": hashlib.sha256(value).hexdigest(),
            "size_bytes": len(value),
        }
        for key, value in (("weights-a", b"weights-a"), ("weights-b", b"weights-b"))
    ]
    model = ModelDefinition.model_validate(model_raw)
    model_digest = document_sha256(model.model_dump(mode="json"))

    recipe_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    recipe_raw["models"][0]["model"]["content_sha256"] = model_digest
    recipe_raw["models"][0]["files"] = [
        {
            "id": key,
            "file_id": key,
            "roles": ["entrypoint"],
            "mount": {"target": f"/models/{key}.bin"},
        }
        for key in ("weights-a", "weights-b")
    ]
    recipe = RecipeDefinition.model_validate(recipe_raw)
    now = datetime.now(UTC)
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'availability.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: now,
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
    )
    recipe_revision_id = catalog.import_recipe_library(
        "test",
        library_commit="0" * 40,
        source_path="recipes/synthetic-tiny.json",
        document=recipe.model_dump(mode="json"),
        expected_content_sha256=document_sha256(recipe.model_dump(mode="json")),
        dependency_documents=[model.model_dump(mode="json")],
        source_bundle_sha256="c" * 64,
    ).id

    client = httpx2.Client(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
    )
    cache_root = tmp_path / "model-cache"
    cache = ModelCacheService(
        sessions,
        cache_root,
        reserve_bytes=0,
        max_parallel_downloads=2,
        fixture_sources=True,
        http_client=client,
    )
    preview = cache.download_preview(recipe_revision_id=recipe_revision_id)
    seeded = cache.start_download(
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000801",
        plan_digest=str(preview["plan_digest"]),
        recipe_revision_id=recipe_revision_id,
    )
    _drain(cache, seeded.id)

    image_archive = b"healthy runtime image"
    image_archive_sha256 = hashlib.sha256(image_archive).hexdigest()

    storage = FilesystemRuntimeImageStorage(tmp_path / "image-cache")
    image_builds: list[str] = []

    def builder(*_args: object, claim: Any, **_kwargs: object):
        # The production builder reuses a verified archive; only a missing
        # archive is built.
        archive = storage.root / image_archive_sha256
        if not archive.exists():
            image_builds.append(str(claim.operation_id))
            archive.write_bytes(image_archive)
        return {
            "state": "succeeded",
            "build_id": "00000000-0000-4000-8000-000000000900",
            "build_input_sha256": "f" * 64,
            "image_digest": "sha256:" + "e" * 64,
            "oci_layout_sha256": image_archive_sha256,
            "image_bytes": len(image_archive),
        }

    class ImageTransport:
        def inspect_archive(
            self,
            archive: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            return PulledImageEvidence(
                manifest_digest="sha256:" + "e" * 64,
                config_id="sha256:" + "f" * 64,
                local_reference="docker-archive:" + str(archive),
                architecture=expected_architecture,
                runtime_interface=expected_runtime_interface,
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, *, force=False: (
            recipe,
            {
                "architecture": "linux/arm64",
                "interface": "vonk.runtime.v1",
                "image_bytes": len(image_archive),
                "build_input_sha256": "f" * 64,
            },
        ),
        transport=ImageTransport(),
        builder=builder,
        # This test is about model recovery; image authorization has its own.
        receipt_writer=lambda *_args: None,
        model_cache=cache,
        clock=lambda: datetime.now(UTC),
        max_parallel_builds=2,
    )
    seeded_parent = service.start(
        recipe_revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000802",
    )
    # Admission queues intent; the worker resolves/adopts its model child.
    assert service.run_pending(limit=1) == 1
    seeded_parent = service.get(seeded_parent.id)
    assert seeded_parent.state == "succeeded"
    assert seeded_parent.model_child is not None
    assert seeded_parent.model_child["id"] == seeded.id
    assert len(image_builds) == 1

    missing_digest = hashlib.sha256(b"weights-a").hexdigest()
    retained_digest = hashlib.sha256(b"weights-b").hexdigest()
    missing_path = cache_root / "objects" / missing_digest[:2] / missing_digest
    retained_path = cache_root / "objects" / retained_digest[:2] / retained_digest
    retained_stat = retained_path.stat()
    missing_path.unlink()
    requests.clear()

    new_parent = service.start(
        recipe_revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000803",
    )
    resumed_parent = service.start(
        recipe_revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000804",
    )

    assert service.run_pending(limit=2) == 2
    new_parent = service.get(new_parent.id)
    assert new_parent.model_child is not None
    assert new_parent.model_child["id"] != seeded.id
    resumed_waiting = service.get(resumed_parent.id)
    assert resumed_waiting.state == "partial"
    assert resumed_waiting.model_child is not None
    assert resumed_waiting.model_child["id"] == new_parent.model_child["id"]
    assert len(image_builds) == 1

    _drain(cache, str(new_parent.model_child["id"]))
    assert requests == ["/models/synthetic-tiny/resolve/" + "0" * 40 + "/weights-a.bin"]
    assert retained_path.read_bytes() == b"weights-b"
    assert retained_path.stat().st_ino == retained_stat.st_ino
    assert missing_path.read_bytes() == b"weights-a"

    with sessions.begin() as session:
        for operation_id in (resumed_parent.id, new_parent.id):
            operation = session.get(Job, operation_id)
            assert operation is not None
            operation.payload = dict(operation.payload) | {
                "retry_after_at": "2000-01-01T00:00:00+00:00"
            }
    assert service.run_pending(limit=2) == 2
    assert service.get(resumed_parent.id).state == "succeeded"
    assert service.get(new_parent.id).state == "succeeded"
    assert len(image_builds) == 1

    cache.close()
    client.close()
