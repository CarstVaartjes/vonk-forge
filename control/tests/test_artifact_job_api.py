from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.artifact_job_api import install_artifact_job_routes
from vonk_control.auth import Actor

JOB_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class _ArtifactJobView:
    id: str
    state: str


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
    assert upload.json() == {"id": JOB_ID, "state": "draft"}
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
