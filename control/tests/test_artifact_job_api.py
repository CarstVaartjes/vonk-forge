from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.artifact_job_api import ArtifactJobCreate, install_artifact_job_routes
from vonk_control.auth import Actor

JOB_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class _ArtifactJobView:
    id: str
    state: str
    run_id: str = "00000000-0000-4000-8000-000000000002"
    operation_id: str | None = None
    interface: str = "image-job"
    contract_sha256: str = "a" * 64
    compiled_contract: dict[str, object] = field(
        default_factory=lambda: {"engine": {"future_argument": {"enabled": True}}}
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
    result_evidence: dict[str, object] | None = None
    status_reason: str | None = None
    timeout_seconds: int = 60
    created_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)



class _TransferService:
    def __init__(self, result_path: Path) -> None:
        self.result_path = result_path
        self.upload: dict[str, object] | None = None

    async def put_input_stream(self, job_id: str, **values: object) -> _ArtifactJobView:
        chunks = values.pop("chunks")
        self.upload = {
            "job_id": job_id,
            **values,
            "content": b"".join([chunk async for chunk in chunks]),
        }
        return _ArtifactJobView(id=job_id, state="draft")

    def result_blob(self, job_id: str, sha256: str):
        assert job_id == JOB_ID
        assert sha256 == hashlib.sha256(self.result_path.read_bytes()).hexdigest()
        return (
            self.result_path,
            "image/png",
            "result.png",
            self.result_path.stat().st_size,
        )


def _client(tmp_path: Path) -> tuple[TestClient, _TransferService]:
    result_path = tmp_path / "result.png"
    result_path.write_bytes(b"png-result")
    service = _TransferService(result_path)
    app = FastAPI()
    install_artifact_job_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    return TestClient(app), service


def test_artifact_transfer_openapi_declares_binary_streams(tmp_path: Path) -> None:
    client, _service = _client(tmp_path)
    paths = client.get("/openapi.json").json()["paths"]
    components = client.get("/openapi.json").json()["components"]["schemas"]

    status = paths["/api/v1/artifact-jobs/{job_id}"]["get"]
    assert status["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ArtifactJobResponse"
    }
    assert components["ArtifactJobResponse"]["additionalProperties"] is False
    assert components["ArtifactJobResponse"]["properties"]["state"]["enum"] == [
        "draft",
        "ready",
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelling",
        "cancelled",
        "waiting-for-operator",
    ]

    upload = paths["/api/v1/artifact-jobs/{job_id}/inputs/{name}"]["put"]
    assert upload["x-vonk-streaming-transport"] is True
    assert upload["requestBody"] == {
        "required": True,
        "content": {
            "application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}
            }
        },
    }

    download = paths["/api/v1/artifact-jobs/{job_id}/results/{sha256}"]["get"]
    assert download["x-vonk-streaming-transport"] is True
    assert download["responses"]["200"]["content"] == {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
    }
    assert "application/json" not in download["responses"]["200"]["content"]


def test_artifact_transfer_routes_preserve_raw_bytes_and_result_media_type(
    tmp_path: Path,
) -> None:
    client, service = _client(tmp_path)
    content = b"raw-input"
    digest = hashlib.sha256(content).hexdigest()

    upload = client.put(
        f"/api/v1/artifact-jobs/{JOB_ID}/inputs/input.png",
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
    assert upload_document["state"] == "draft"
    assert upload_document["interface"] == "image-job"
    assert upload_document["output_limits"]["allowed_media_types"] == ["image/png"]
    assert upload_document["compiled_contract"]["engine"]["future_argument"] == {
        "enabled": True
    }
    assert upload_document["created_at"].endswith("Z")
    assert service.upload == {
        "job_id": JOB_ID,
        "name": "input.png",
        "media_type": "image/png",
        "expected_sha256": digest,
        "content_length": len(content),
        "content": content,
    }

    result_digest = hashlib.sha256(b"png-result").hexdigest()
    download = client.get(f"/api/v1/artifact-jobs/{JOB_ID}/results/{result_digest}")
    assert download.status_code == 200
    assert download.content == b"png-result"
    assert download.headers["content-type"] == "image/png"
    assert download.headers["x-content-sha256"] == result_digest


def test_artifact_job_boundary_rejects_unknown_top_level_and_scalar_coercion(
    tmp_path: Path,
) -> None:
    client, _service = _client(tmp_path)
    body = {
        "interface": "image-job",
        "parameters": {"future_argument": {"enabled": True}},
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
    assert accepted.parameters == {"future_argument": {"enabled": True}}

    unknown_field = client.post(
        f"/api/v1/recipes/runs/{JOB_ID}/artifact-jobs",
        json={**body, "unexpected": True},
    )
    assert unknown_field.status_code == 422

    coerced_scalar = client.post(
        f"/api/v1/recipes/runs/{JOB_ID}/artifact-jobs",
        json={**body, "timeout_seconds": "60"},
    )
    assert coerced_scalar.status_code == 422
