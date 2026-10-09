from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.responses import Response
from vonk_control.artifact_job_api import ArtifactJobCreate, install_artifact_job_routes
from vonk_control.artifact_job_evidence import (
    ArtifactJobResultEvidence,
    dump_result_evidence,
    read_result_evidence,
)
from vonk_control.artifact_jobs import (
    ArtifactJobResponse,
    ArtifactJobService,
    ArtifactJobView,
    CompiledArtifactContract,
)
from vonk_control.auth import Actor
from vonk_control.models import ArtifactJobBlob, ArtifactJobFile
from vonk_control.strict_json import ControllerAPIRoute

from cluster_profiles.generated_control.models.artifact_job_response import (
    ArtifactJobResponse as ClientArtifactJobResponse,
)

JOB_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
REQUEST_ID = "00000000-0000-4000-8000-000000000003"


@dataclass(frozen=True, slots=True)
class _ArtifactJobView(ArtifactJobView):
    id: str
    state: str | None = None
    preparation: str | None = "draft"
    cancel_requested_at: datetime | None = None
    run_id: str = "00000000-0000-4000-8000-000000000002"
    operation_id: str | None = None
    submit_request_id: str | None = None
    interface: str = "image-job"
    contract_sha256: str = "a" * 64
    compiled_contract: CompiledArtifactContract = field(
        default_factory=lambda: CompiledArtifactContract.model_validate(
            {
                "schema_version": 1,
                "interface": "image-job",
                "input": {
                    "required": False,
                    "media_types": [],
                    "max_bytes": 0,
                    "slots": [],
                },
                "parameters": [],
                "output": {
                    "path": "/outputs",
                    "max_total_bytes": 1024,
                    "slots": [
                        {
                            "id": "image",
                            "label": "Image",
                            "description": "Generated image",
                            "media_types": ["image/png"],
                            "extensions": [".png"],
                            "min_files": 0,
                            "max_files": 1,
                            "max_file_bytes": 1024,
                            "max_total_bytes": 1024,
                        }
                    ],
                },
                "output_limits": {
                    "max_files": 1,
                    "max_file_bytes": 1024,
                    "max_total_bytes": 1024,
                    "allowed_media_types": ["image/png"],
                },
                "max_timeout_seconds": 3600,
            }
        )
    )
    input_manifest_sha256: str = "b" * 64
    input_total_bytes: int = 0
    input_declarations: tuple[dict[str, object], ...] = ()
    input_files: tuple[dict[str, object], ...] = ()
    output_limits: dict[str, object] = field(
        default_factory=lambda: {
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 1024,
            "allowed_media_types": ["image/png"],
        }
    )
    output_manifest_sha256: str | None = None
    output_files: tuple[dict[str, object], ...] = ()
    result_evidence: ArtifactJobResultEvidence | None = None
    status_reason: str | None = None
    timeout_seconds: int = 60
    created_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)


class _TransferService(ArtifactJobService):
    def __init__(
        self, result_path: Path, *, result_media_types: dict[str, str] | None = None
    ) -> None:
        self.result_path = result_path
        self.result_media_types = result_media_types or {"result.png": "image/png"}
        self.upload: dict[str, object] | None = None

    async def put_input_stream(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        expected_sha256: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
    ) -> ArtifactJobView:
        self.upload = {
            "job_id": job_id,
            "name": name,
            "media_type": media_type,
            "expected_sha256": expected_sha256,
            "content_length": content_length,
            "content": b"".join([chunk async for chunk in chunks]),
        }
        return _ArtifactJobView(id=job_id)

    def result_blob(
        self, job_id: str, name: str, sha256: str
    ) -> tuple[Path, str, str, int]:
        assert job_id == JOB_ID
        if (
            name not in self.result_media_types
            or sha256 != hashlib.sha256(self.result_path.read_bytes()).hexdigest()
        ):
            raise KeyError(name)
        return (
            self.result_path,
            self.result_media_types[name],
            name,
            self.result_path.stat().st_size,
        )


def _client(
    tmp_path: Path, *, result_media_types: dict[str, str] | None = None
) -> tuple[TestClient, _TransferService]:
    result_path = tmp_path / "result.png"
    result_path.write_bytes(b"png-result")
    service = _TransferService(result_path, result_media_types=result_media_types)
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute
    install_artifact_job_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    return TestClient(app), service


def test_artifact_status_roundtrips_through_the_generated_consumer(
    tmp_path: Path,
) -> None:
    client, _service = _client(tmp_path)
    response = client.get(f"/api/artifact-jobs/{JOB_ID}")
    assert response.status_code == 200
    canonical = ArtifactJobResponse.model_validate_json(response.content)
    consumed = ClientArtifactJobResponse.from_dict(response.json())
    assert consumed.id == canonical.id == JOB_ID
    assert consumed.contract_sha256 == canonical.contract_sha256
    assert consumed.input_total_bytes == canonical.input_total_bytes == 0
    assert canonical.operation_id is None
    assert (
        ArtifactJobResponse.model_validate_json(json.dumps(consumed.to_dict()))
        == canonical
    )


def test_artifact_transfer_routes_preserve_raw_bytes_and_result_media_type(
    tmp_path: Path,
) -> None:
    client, service = _client(
        tmp_path,
        result_media_types={
            "result.png": "image/png",
            "metadata.json": "application/json",
        },
    )
    content = b"raw-input"
    digest = hashlib.sha256(content).hexdigest()

    upload = client.put(
        f"/api/artifact-jobs/{JOB_ID}/inputs/input.png",
        content=content,
        headers={
            "Content-Type": "image/png",
            "X-Content-SHA256": digest,
            "Content-Length": str(len(content)),
        },
    )
    assert upload.status_code == 200
    upload_document = upload.json()
    assert upload_document["id"] == JOB_ID
    assert upload_document["state"] is None
    assert upload_document["preparation"] == "draft"
    assert upload_document["interface"] == "image-job"
    assert upload_document["output_limits"]["allowed_media_types"] == ["image/png"]
    assert upload_document["compiled_contract"]["input"]["slots"] == []
    assert upload_document["compiled_contract"]["output"]["slots"][0]["id"] == "image"
    assert "engine" not in upload_document["compiled_contract"]
    assert upload_document["created_at"].endswith("Z")
    expected = ArtifactJobResponse.model_validate_json(upload.content)
    optional_nulls = {
        name: None
        for name, field in ArtifactJobResponse.model_fields.items()
        if not field.is_required()
        and field.default is None
        and name != "preparation"  # a draft carries its stage
    }
    assert not optional_nulls.keys() & upload_document.keys()
    # Consume actual emitted output in the generated client, then serialize back
    # through its public API. Declared optional omission and null agree without
    # losing meaningful zero, false, empty collections or engine extensions.
    for document in (upload_document, {**upload_document, **optional_nulls}):
        parsed = ClientArtifactJobResponse.from_dict(document)
        assert parsed.input_total_bytes == 0
        assert (
            ArtifactJobResponse.model_validate_json(json.dumps(parsed.to_dict()))
            == expected
        )
    assert service.upload == {
        "job_id": JOB_ID,
        "name": "input.png",
        "media_type": "image/png",
        "expected_sha256": digest,
        "content_length": len(content),
        "content": content,
    }

    result_digest = hashlib.sha256(b"png-result").hexdigest()
    for name, media_type in (
        ("result.png", "image/png"),
        ("metadata.json", "application/json"),
    ):
        download = client.get(
            f"/api/artifact-jobs/{JOB_ID}/results/{name}/{result_digest}"
        )
        assert download.status_code == 200
        assert download.content == b"png-result"
        assert download.headers["content-type"] == media_type
        assert download.headers["content-disposition"] == (
            f'attachment; filename="{name}"'
        )
        assert download.headers["x-content-sha256"] == result_digest

    assert (
        client.get(
            f"/api/artifact-jobs/{JOB_ID}/results/unclaimed.png/{result_digest}"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/artifact-jobs/{JOB_ID}/results/result.png/{'0' * 64}"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/artifact-jobs/{JOB_ID}/results/bad%20name/{result_digest}"
        ).status_code
        == 422
    )
    # The old digest-only path is intentionally not a compatibility alias.
    assert (
        client.get(f"/api/artifact-jobs/{JOB_ID}/results/{result_digest}").status_code
        == 404
    )


def test_artifact_job_boundary_rejects_unknown_top_level_and_scalar_coercion(
    tmp_path: Path,
) -> None:
    client, _service = _client(tmp_path)
    body = {
        "interface": "image-job",
        "parameters": {"future_argument": True},
        "inputs": [],
        "output_limits": {
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 1024,
            "allowed_media_types": ["image/png"],
        },
        "timeout_seconds": 60,
    }
    accepted = ArtifactJobCreate.model_validate(body)
    assert accepted.parameters == {"future_argument": True}
    # A parameter is a scalar; a nested object is refused at the boundary.
    nested = client.post(
        f"/api/recipe/runs/{JOB_ID}/artifact-jobs",
        json={**body, "parameters": {"future_argument": {"enabled": True}}},
    )
    assert nested.status_code == 422

    unknown_field = client.post(
        f"/api/recipe/runs/{JOB_ID}/artifact-jobs",
        json={**body, "unexpected": True},
    )
    assert unknown_field.status_code == 422

    coerced_scalar = client.post(
        f"/api/recipe/runs/{JOB_ID}/artifact-jobs",
        json={**body, "timeout_seconds": "60"},
    )
    assert coerced_scalar.status_code == 422


def test_real_artifact_service_recovers_lost_create_and_streams_declared_bytes(
    tmp_path: Path,
) -> None:
    from .test_artifact_jobs import artifact_create_request, running_artifact_service

    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    request = artifact_create_request(run_id, REQUEST_ID)
    body = {
        key: value
        for key, value in request.items()
        if key not in {"run_id", "actor", "request_id"}
    }
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute
    install_artifact_job_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    lost_response = [True]

    @app.middleware("http")
    async def drop_first_create_receipt(request: Request, call_next):
        response = await call_next(request)
        if (
            lost_response[0]
            and request.method == "POST"
            and request.url.path == f"/api/recipe/runs/{run_id}/artifact-jobs"
            and response.status_code == 201
        ):
            lost_response[0] = False
            return Response(status_code=500, content=b"accepted; response lost")
        return response

    client = TestClient(app, raise_server_exceptions=False)
    create_path = f"/api/recipe/runs/{run_id}/artifact-jobs"

    missing_identity = client.post(create_path, json=body)
    assert missing_identity.status_code == 422
    invalid_identity = client.post(
        create_path,
        json=body,
        headers={"X-Request-ID": "not-a-uuid"},
    )
    assert invalid_identity.status_code == 422
    assert client.get(f"/api/artifact-jobs/requests/{REQUEST_ID}").status_code == 404

    lost = client.post(
        create_path,
        json=body,
        headers={"X-Request-ID": REQUEST_ID},
    )
    assert lost.status_code == 500

    recovered = client.get(f"/api/artifact-jobs/requests/{REQUEST_ID}")
    assert recovered.status_code == 200
    draft_id = recovered.json()["id"]
    assert recovered.json()["state"] is None
    assert recovered.json()["preparation"] == "draft"
    replay = client.post(
        create_path,
        json=body,
        headers={"X-Request-ID": REQUEST_ID},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == draft_id

    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    uploaded = client.put(
        f"/api/artifact-jobs/{draft_id}/inputs/input.png",
        content=content,
        headers={
            "Content-Type": "image/png",
            "Content-Length": str(len(content)),
            "X-Content-SHA256": digest,
        },
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["input_files"] == body["inputs"]
    finalized = client.post(f"/api/artifact-jobs/{draft_id}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["state"] is None
    assert finalized.json()["preparation"] == "ready"
    for headers in ({}, {"X-Request-ID": "not-a-uuid"}):
        assert (
            client.post(
                f"/api/artifact-jobs/{draft_id}/submit", headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/artifact-jobs/{draft_id}/cancel",
                json={"reason": "stop"},
                headers=headers,
            ).status_code
            == 422
        )
    assert service.get(draft_id).preparation == "ready"
    with sessions() as session:
        file = session.scalar(
            select(ArtifactJobFile).where(
                ArtifactJobFile.artifact_job_id == draft_id,
                ArtifactJobFile.direction == "input",
                ArtifactJobFile.name == "input.png",
            )
        )
        blob = session.get(ArtifactJobBlob, digest)
        assert file is not None and blob is not None
        assert (file.media_type, file.size_bytes, file.blob_sha256) == (
            "image/png",
            len(content),
            digest,
        )
        blob_path = service._blob_store.resolve(
            blob.storage_key, blob.sha256, blob.size_bytes
        )
    assert blob_path is not None and blob_path.read_bytes() == content


def test_result_evidence_preserves_current_values_without_field_presence() -> None:
    """Evidence persistence must serialize values, rather than field-set history.

    This contract has no collection defaults; mutate its backing values in place
    to exercise the same distinction without inventing production evidence fields.
    """
    evidence = ArtifactJobResultEvidence()
    vars(evidence).update(recoverable=False, active_scope_may_remain=False)
    assert not evidence.model_fields_set
    document = dump_result_evidence(evidence)
    assert document is not None
    assert document["recoverable"] is False
    assert document["active_scope_may_remain"] is False
    assert read_result_evidence(json.loads(json.dumps(document))) == evidence


def test_capacity_unknown_releases_upload_and_fresh_draft_is_admitted(tmp_path):
    """Catches translating storage shortage into a conflict at the HTTP boundary."""
    from vonk_control.artifact_blob_store import ArtifactBlobStore

    from .test_artifact_jobs import (
        artifact_create_request,
        create_artifact_job,
        running_artifact_service,
    )

    _, _, _, service, run_id, _ = running_artifact_service(tmp_path)
    store = ArtifactBlobStore(tmp_path / "quota", max_stored_bytes=4)
    service._blob_store = store
    occupied = store.put_bytes(
        hashlib.sha256(b"xx").hexdigest(), b"xx", maximum_bytes=4
    )
    draft = create_artifact_job(service, **artifact_create_request(run_id, REQUEST_ID))
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute
    install_artifact_job_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    client = TestClient(app)
    payload = b"png"
    headers = {
        "Content-Type": "image/png",
        "Content-Length": str(len(payload)),
        "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
    }
    unavailable = client.put(
        f"/api/artifact-jobs/{draft.id}/inputs/input.png",
        content=payload,
        headers=headers,
    )
    assert unavailable.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert store.usage().in_flight_uploads == 0
    assert not service.get(draft.id).input_files
    fresh = create_artifact_job(
        service,
        **artifact_create_request(
            run_id,
            "00000000-0000-4000-8000-000000000004",
        ),
    )
    assert fresh.id != draft.id
    store.delete(occupied.storage_key, occupied.sha256)
    recovered = client.put(
        f"/api/artifact-jobs/{fresh.id}/inputs/input.png",
        content=payload,
        headers=headers,
    )
    assert recovered.status_code == 200
    assert (
        service.get(fresh.id).input_files[0].sha256
        == hashlib.sha256(payload).hexdigest()
    )
    assert store.usage().in_flight_uploads == 0


def test_library_upload_reobserves_capacity_after_releasing_holds(
    tmp_path, monkeypatch
):
    """Catches an ended library upload with no automatic bounded observation."""
    from vonk_control.artifact_blob_store import ArtifactBlobStore
    from vonk_control.artifact_jobs import inputs
    from vonk_control.bounded_retry import bounded_attempts

    from .test_artifact_jobs import (
        artifact_create_request,
        create_artifact_job,
        running_artifact_service,
    )

    _, _, _, service, run_id, _ = running_artifact_service(tmp_path)
    store = ArtifactBlobStore(tmp_path / "quota", max_stored_bytes=4)
    service._blob_store = store
    occupied = store.put_bytes(
        hashlib.sha256(b"xx").hexdigest(), b"xx", maximum_bytes=4
    )
    draft = create_artifact_job(service, **artifact_create_request(run_id, REQUEST_ID))

    def capacity_recovers(_pause):
        assert store.usage().in_flight_uploads == 0
        store.delete(occupied.storage_key, occupied.sha256)

    monkeypatch.setattr(
        inputs, "bounded_attempts", lambda: bounded_attempts(sleep=capacity_recovers)
    )
    uploaded = service.put_input(
        draft.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=hashlib.sha256(b"png").hexdigest(),
        content=b"png",
    )
    assert uploaded.input_files[0].sha256 == hashlib.sha256(b"png").hexdigest()
    assert store.usage().in_flight_uploads == 0
