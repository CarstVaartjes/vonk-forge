"""NAS ingest of a model file the source publishes only as ordered parts.

The fake Hugging Face is an ``httpx2.MockTransport`` that serves each part by
URL (with ``Range`` support) and records every request, so the tests can state
exactly which bytes a resume or a cache hit did and did not fetch.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import LifecycleState
from vonk_control.catalog_entities import _execution_projection
from vonk_control.model_cache import (
    ArtifactPart,
    ArtifactSetManifest,
    ArtifactSpec,
    ModelCacheService,
    _validate_artifact,
)
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision
from vonk_forge_contracts import ModelDefinition, document_sha256

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
MODEL = "c" * 64
REVISION = "0" * 40
HOST = "https://huggingface.co/owner/split-model/resolve/" + REVISION + "/"
PART_SIZES = (1_500_000, 1_200_000, 300_000)
KEY = "00000000-0000-4000-8000-0000000000{:02d}"


def _payload() -> bytes:
    return b"".join(bytes([65 + index]) * size for index, size in enumerate(PART_SIZES))


def _split(whole: bytes) -> list[bytes]:
    edges = [0]
    for size in PART_SIZES:
        edges.append(edges[-1] + size)
    return [whole[edges[i] : edges[i + 1]] for i in range(len(PART_SIZES))]


def _part_name(index: int) -> str:
    return f"model-00005.safetensors.part{index:02d}"


@dataclass
class FakeHub:
    """Serves the parts by name; records each request's path and range."""

    files: dict[str, bytes]
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    unavailable: set[str] = field(default_factory=set)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        header = request.headers.get("range")
        self.requests.append((path, header))
        if path in self.unavailable:
            return httpx2.Response(503, request=request)
        data = self.files.get(path)
        if data is None:
            return httpx2.Response(404, request=request)
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", header or "")
        if match is None:
            return httpx2.Response(200, request=request, content=data)
        start = int(match[1])
        end = int(match[2]) if match[2] else len(data) - 1
        return httpx2.Response(
            206,
            request=request,
            content=data[start : end + 1],
            headers={"content-range": f"bytes {start}-{end}/{len(data)}"},
        )

    def fetched(self, path: str) -> int:
        return sum(1 for name, _range in self.requests if name == path)


def _artifact(
    whole: bytes, pieces: list[bytes], *, whole_sha256: str | None = None
) -> dict[str, object]:
    return {
        "id": "shard",
        "path": "model-00005.safetensors",
        "kind": "huggingface.file",
        "repository": "https://huggingface.co/owner/split-model",
        "source": HOST + "model-00005.safetensors",
        "revision": REVISION,
        "sha256": whole_sha256 or hashlib.sha256(whole).hexdigest(),
        "download_bytes": len(whole),
        "roles": ["weights"],
        "model_content_sha256": MODEL,
        "parts": [
            {
                "path": _part_name(index),
                "source": HOST + _part_name(index),
                "sha256": hashlib.sha256(piece).hexdigest(),
                "download_bytes": len(piece),
            }
            for index, piece in enumerate(pieces)
        ],
    }


@pytest.fixture
def hub():
    return FakeHub({_part_name(i): piece for i, piece in enumerate(_split(_payload()))})


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def service(tmp_path: Path, sessions, hub: FakeHub):
    client = httpx2.Client(
        transport=httpx2.MockTransport(hub.handler), follow_redirects=False
    )
    cache = ModelCacheService(
        sessions,
        tmp_path / "nas-cache",
        reserve_bytes=0,
        http_client=client,
        fixture_sources=True,
    )
    yield cache
    cache.close()
    client.close()


def _start(service: ModelCacheService, artifact, key: str, **kwargs):
    preview = service.download_preview(model_content_sha256=MODEL, artifacts=[artifact])
    return service.start_download(
        actor="test",
        request_key=key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=MODEL,
        artifacts=[artifact],
        **kwargs,
    )


def _leftovers(service: ModelCacheService) -> list[Path]:
    return sorted((service.root / "partials").rglob("*.part"))


def _spec(whole: bytes, *, split_parts: bool = True) -> ArtifactSpec:
    pieces = _split(whole)
    return ArtifactSpec(
        key="artifact-111111111111-shard",
        artifact_id="shard",
        path="model-00005.safetensors",
        kind="huggingface.file",
        repository="https://huggingface.co/owner/split-model",
        source=HOST + "model-00005.safetensors",
        revision=REVISION,
        sha256=hashlib.sha256(whole).hexdigest(),
        expected_bytes=len(whole),
        roles=("weights",),
        parts=(
            tuple(
                ArtifactPart(
                    path=_part_name(index),
                    source=HOST + _part_name(index),
                    sha256=hashlib.sha256(piece).hexdigest(),
                    expected_bytes=len(piece),
                )
                for index, piece in enumerate(pieces)
            )
            if split_parts
            else None
        ),
    )


def test_parts_are_assembled_into_one_verified_object_and_nothing_is_left(
    service, hub
) -> None:
    whole = _payload()
    artifact = _artifact(whole, _split(whole))
    operation = _start(service, artifact, KEY.format(1))
    service.run_pending()
    finished = service.get_operation(operation.id)
    assert finished.state == "succeeded", finished.last_error
    digest = str(artifact["sha256"])
    assert service._object_path(digest).read_bytes() == whole
    assert service._object_is_available(digest, len(whole))
    # No part is a cache object, and no part or temp bytes outlive the ingest.
    assert not any(
        service._object_path(hashlib.sha256(piece).hexdigest()).exists()
        for piece in _split(whole)
    )
    assert _leftovers(service) == []
    assert [name for name, _range in hub.requests] == [_part_name(i) for i in range(3)]
    assert finished.progress["downloaded_bytes"] == len(whole)
    assert finished.progress["expected_bytes"] == len(whole)


def test_a_corrupt_part_is_discarded_and_refetched_without_touching_earlier_parts(
    service, hub
) -> None:
    whole = _payload()
    pieces = _split(whole)
    artifact = _artifact(whole, pieces)
    good = hub.files[_part_name(1)]
    hub.files[_part_name(1)] = b"Z" * len(good)
    operation = _start(service, artifact, KEY.format(2))
    service.run_pending()
    failed = service.get_operation(operation.id)
    assert failed.state == "queued"  # integrity failures retry, never publish
    assert not service._object_path(str(artifact["sha256"])).exists()
    # The bad bytes are cut off the assembled file and the part is deleted;
    # only the verified first part's bytes are retained.
    assert [path.stat().st_size for path in _leftovers(service)] == [len(pieces[0])]
    hub.files[_part_name(1)] = good
    hub.requests.clear()
    service._run_download(operation.id, force=False)
    assert service.get_operation(operation.id).state == "succeeded"
    assert service._object_path(str(artifact["sha256"])).read_bytes() == whole
    assert [name for name, _range in hub.requests] == [_part_name(1), _part_name(2)]
    assert _leftovers(service) == []


def test_a_wrong_whole_digest_never_publishes_and_discards_the_assembly(
    service, hub
) -> None:
    whole = _payload()
    artifact = _artifact(whole, _split(whole), whole_sha256="9" * 64)
    operation = _start(service, artifact, KEY.format(3))
    service.run_pending()
    failed = service.get_operation(operation.id)
    assert failed.state == "queued"
    assert not service._object_path("9" * 64).exists()
    assert _leftovers(service) == []
    fresh = _start(service, _artifact(whole, _split(whole)), KEY.format(73))
    service.run_pending(limit=2)
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
    assert service._object_path(hashlib.sha256(whole).hexdigest()).read_bytes() == whole


def test_interrupted_assembly_resumes_from_the_appended_prefix(service, hub) -> None:
    whole = _payload()
    pieces = _split(whole)
    artifact = _artifact(whole, pieces)
    hub.unavailable.add(_part_name(2))
    operation = _start(service, artifact, KEY.format(4))
    service.run_pending()
    assert service.get_operation(operation.id).state == "queued"
    retained = _leftovers(service)
    assert [path.stat().st_size for path in retained] == [
        len(pieces[0]) + len(pieces[1])
    ]
    # A crash mid-append leaves a torn tail beyond the last whole part.
    with retained[0].open("ab") as torn:
        torn.write(b"torn tail that is not a part")
    hub.unavailable.clear()
    hub.requests.clear()
    service._run_download(operation.id, force=False)
    assert service.get_operation(operation.id).state == "succeeded"
    assert service._object_path(str(artifact["sha256"])).read_bytes() == whole
    # Only the missing part came over the network.
    assert [name for name, _range in hub.requests] == [_part_name(2)]
    assert _leftovers(service) == []


def test_a_retained_complete_part_is_appended_after_an_interrupted_transfer(
    service, hub
) -> None:
    whole = _payload()
    artifact = _artifact(whole, _split(whole))
    interrupted = _start(
        service, artifact, KEY.format(5), interrupt_after_bytes=1_100_000
    )
    assert interrupted.state == LifecycleState.BACKOFF
    assert service.resume_operations() == 1
    hub.requests.clear()
    service.run_pending()
    assert service.get_operation(interrupted.id).state == "succeeded"
    assert service._object_path(str(artifact["sha256"])).read_bytes() == whole
    # The first part was already complete on disk; it is not fetched again.
    assert hub.fetched(_part_name(0)) == 0


def test_a_cached_whole_file_skips_every_part_download(service, hub) -> None:
    whole = _payload()
    artifact = _artifact(whole, _split(whole))
    digest = str(artifact["sha256"])
    # The same bytes, cached earlier by another model that publishes them whole.
    target = service._object_path(digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(whole)
    other = replace(_spec(whole, split_parts=False), path="model-00047.safetensors")
    service._write_object_receipt(other, NOW)
    preview = service.download_preview(model_content_sha256=MODEL, artifacts=[artifact])
    assert preview["new_bytes"] == 0
    assert preview["already_cached_bytes"] == len(whole)
    operation = _start(service, artifact, KEY.format(6))
    service.run_pending()
    assert service.get_operation(operation.id).state == "succeeded"
    assert hub.requests == []


def test_preview_counts_the_largest_part_as_transient_disk(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = _payload()
    artifact = _artifact(whole, _split(whole))
    monkeypatch.setattr(service, "free_bytes", lambda: len(whole))
    blocked = service.download_preview(model_content_sha256=MODEL, artifacts=[artifact])
    assert blocked["blockers"] == ["insufficient-reserved-storage"]
    monkeypatch.setattr(service, "free_bytes", lambda: len(whole) + max(PART_SIZES))
    admitted = service.download_preview(
        model_content_sha256=MODEL, artifacts=[artifact]
    )
    assert admitted["blockers"] == []


def test_manifest_keeps_parts_but_identity_is_the_whole_files_bytes() -> None:
    whole = _payload()

    def manifest(spec: ArtifactSpec) -> ArtifactSetManifest:
        return ArtifactSetManifest(
            model_content_sha256=MODEL,
            recipe_revision_sha256=None,
            model_content_digests=(MODEL,),
            artifacts=(spec,),
        )

    split = ArtifactSetManifest.from_document(manifest(_spec(whole)).document())
    assert split.artifacts[0].parts is not None
    assert len(split.artifacts[0].parts) == 3
    plain = manifest(_spec(whole, split_parts=False))
    assert "parts" not in plain.document()["artifacts"][0]  # type: ignore[index]
    # Reuse and set identity ignore how the source happened to ship the bytes.
    assert split.digest == plain.digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda parts: parts[:1],
        lambda parts: [parts[0], parts[0]],
        lambda parts: [*parts[:2], replace(parts[2], expected_bytes=1)],
        lambda parts: [replace(parts[0], path="model-00005.safetensors"), *parts[1:]],
        lambda parts: [replace(parts[0], path="../escape"), *parts[1:]],
        lambda parts: [replace(parts[0], sha256="ZZ"), *parts[1:]],
    ],
)
def test_malformed_parts_are_refused_before_any_transfer(mutate) -> None:
    spec = _spec(_payload())
    assert spec.parts is not None
    with pytest.raises(Exception) as _ending:
        _validate_artifact(replace(spec, parts=tuple(mutate(list(spec.parts)))))
    with pytest.raises(Exception) as _ending:
        _validate_artifact(replace(spec, kind="http.file"))
    _validate_artifact(spec)


def test_catalog_model_with_parts_resolves_and_downloads_per_part_sources(
    service, sessions, hub
) -> None:
    whole = _payload()
    pieces = _split(whole)
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = "owner"
    document["identity"]["slug"] = "split-model"
    document["identity"]["model"]["publisher"] = "owner"
    document["identity"]["model"]["slug"] = "split-model"
    document["source"] = {
        "repository": "https://huggingface.co/owner/split-model",
        "revision": REVISION,
    }
    document["files"] = [
        {
            "id": "shard",
            "path": "model-00005.safetensors",
            "sha256": hashlib.sha256(whole).hexdigest(),
            "size_bytes": len(whole),
            "roles": ["weights"],
            "parts": [
                {
                    "path": _part_name(index),
                    "sha256": hashlib.sha256(piece).hexdigest(),
                    "size_bytes": len(piece),
                }
                for index, piece in enumerate(pieces)
            ],
        }
    ]
    document = ModelDefinition.model_validate(document).model_dump(mode="json")
    digest = document_sha256(document)
    with sessions.begin() as session:
        root = CatalogDocument(
            id="00000000-0000-0000-0000-0000000000a1",
            kind="model",
            publisher="owner",
            slug="split-model",
            title="Split model",
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(root)
        session.flush()
        session.add(
            CatalogDocumentRevision(
                id="00000000-0000-0000-0000-0000000000a2",
                document_id=root.id,
                kind="model",
                publisher="owner",
                slug="split-model",
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=digest,
                projected={},
                created_by="test",
                created_at=NOW,
            )
        )
    manifest = service.resolve_artifact_set(model_content_sha256=digest)
    (spec,) = manifest.artifacts
    assert spec.sha256 == hashlib.sha256(whole).hexdigest()
    assert spec.parts is not None
    assert [part.source for part in spec.parts] == [
        HOST + _part_name(index) for index in range(3)
    ]
    assert [part.expected_bytes for part in spec.parts] == list(PART_SIZES)
    preview = service.download_preview(model_content_sha256=digest)
    operation = service.start_download(
        actor="test",
        request_key=KEY.format(9),
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=digest,
    )
    service.run_pending()
    assert service.get_operation(operation.id).state == "succeeded"
    assert service._object_path(spec.sha256).read_bytes() == whole


def test_catalog_artifact_identity_ignores_how_the_source_ships_the_bytes() -> None:
    """Split and whole publication of the same bytes is one artifact key."""

    whole = _payload()
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["files"] = [
        {
            "id": "shard",
            "path": "model-00005.safetensors",
            "sha256": hashlib.sha256(whole).hexdigest(),
            "size_bytes": len(whole),
            "roles": ["weights"],
        }
    ]
    plain = ModelDefinition.model_validate(document)
    document["files"][0]["parts"] = [
        {
            "path": _part_name(index),
            "sha256": hashlib.sha256(piece).hexdigest(),
            "size_bytes": len(piece),
        }
        for index, piece in enumerate(_split(whole))
    ]
    split = ModelDefinition.model_validate(document)
    assert _execution_projection(split) == _execution_projection(plain)


def test_parts_fetched_as_parallel_ranges_are_appended_and_removed(
    service, hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real parts (~17 GB) always take the ranged path; force it for small ones.
    monkeypatch.setattr(
        "vonk_control.model_cache.constants._PARALLEL_RANGE_MIN_BYTES", 100_000
    )
    whole = _payload()
    artifact = _artifact(whole, _split(whole))
    operation = _start(service, artifact, KEY.format(10))
    service.run_pending()
    assert service.get_operation(operation.id).state == "succeeded"
    assert service._object_path(str(artifact["sha256"])).read_bytes() == whole
    ranged = [header for _name, header in hub.requests if header]
    assert len(ranged) >= 9
    assert all(re.fullmatch(r"bytes=\d+-\d+", header) for header in ranged)
    leftovers = [p for p in (service.root / "partials").rglob("*") if p.is_file()]
    assert leftovers == []


def test_a_half_fetched_next_part_counts_toward_resume_bytes(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "vonk_control.model_cache.constants._PARALLEL_RANGE_MIN_BYTES", 100_000
    )
    whole = _payload()
    pieces = _split(whole)
    spec = _spec(whole)
    assert spec.parts is not None
    manifest = ArtifactSetManifest(
        model_content_sha256=MODEL,
        recipe_revision_sha256=None,
        model_content_digests=(MODEL,),
        artifacts=(spec,),
    )
    root = service.root / "partials" / manifest.digest
    root.mkdir(parents=True)
    # Part 0 is appended; part 1 has a 100 KB prefix on disk.
    (root / f"{spec.sha256}.part").write_bytes(whole[: len(pieces[0])])
    (root / f"{spec.parts[1].sha256}.part").write_bytes(pieces[1][:100_000])
    assert service._partial_bytes(manifest.digest, spec) == len(pieces[0]) + 100_000


def test_cache_entry_and_inventory_views_accept_a_split_set(service, hub) -> None:
    whole = _payload()
    artifact = _artifact(whole, _split(whole))
    operation = _start(service, artifact, KEY.format(11))
    service.run_pending()
    finished = service.get_operation(operation.id)
    assert finished.state == "succeeded"
    set_digest = finished.artifact_set_sha256 or ""
    entry = service.get_entry(set_digest)
    assert entry["state"] == "cached"
    assert entry["verified_bytes"] == len(whole)
    (listed,) = entry["artifacts"]  # type: ignore[misc]
    assert listed["sha256"] == artifact["sha256"]  # type: ignore[index]
    assert service.inventory()
    assert (
        service.read_verified_artifact(
            set_digest, str(artifact["sha256"]), "model-00005.safetensors"
        )
        == whole
    )


@pytest.mark.parametrize("damage", ["missing", "short"])
def test_local_part_loss_is_repaired_by_the_current_request(
    service, hub, monkeypatch, damage
):
    whole = _payload()
    artifact = _artifact(whole, _split(whole))
    original_append = service._append_part
    damaged = False

    def append(spec, part, *args, **kwargs):
        nonlocal damaged
        if not damaged:
            damaged = True
            if damage == "missing":
                part.unlink()
            else:
                part.write_bytes(b"torn")
        return original_append(spec, part, *args, **kwargs)

    monkeypatch.setattr(service, "_append_part", append)
    operation = _start(service, artifact, KEY.format(71))
    service.run_pending()
    assert not service._object_path(str(artifact["sha256"])).exists()
    service._run_download(operation.id, force=False)
    assert service.get_operation(operation.id).state == LifecycleState.SUCCEEDED
    assert service._object_path(str(artifact["sha256"])).read_bytes() == whole
    assert _leftovers(service) == []
    fresh = _start(service, artifact, KEY.format(72))
    service.run_pending()
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
