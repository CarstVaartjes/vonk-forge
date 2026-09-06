"""Authenticated controller and mTLS agent routes for artifact recipe jobs."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse
from vonk_agent_protocol import AgentProtocolError

from .artifact_blob_store import ArtifactBlobStore
from .artifact_jobs import (
    MAX_INPUT_FILE_BYTES,
    ArtifactFileDeclaration,
    ArtifactJobCapabilitiesResponse,
    ArtifactJobError,
    ArtifactJobListResponse,
    ArtifactJobResponse,
    ArtifactJobService,
    ArtifactJobView,
    OutputLimits,
)
from .auth import Actor, agent_identity_from_scope
from .operation_api import bounded_error_responses

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_DIGEST = r"^[0-9a-f]{64}$"
_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_MEDIA_TYPE = r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ArtifactJobCreate(StrictModel):
    interface: str = Field(
        pattern=r"^(audio-job|video-job|image-job|mesh-job|artifact-job)$"
    )
    parameters: dict[str, object] = Field(default_factory=dict)
    inputs: list[ArtifactFileDeclaration] = Field(default_factory=list, max_length=32)
    output_limits: OutputLimits
    timeout_seconds: int = Field(ge=1, le=3_600)


class CancelRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=512)


def _service(service: ArtifactJobService | None) -> ArtifactJobService:
    if service is None:
        raise HTTPException(status_code=503, detail="artifact jobs are unavailable")
    return service


def _mutating(actor: Actor) -> None:
    if actor.role not in {"operator", "administrator"}:
        raise HTTPException(status_code=403, detail="insufficient role")


def _view(value: ArtifactJobView) -> ArtifactJobResponse:
    return ArtifactJobResponse.model_validate(value, from_attributes=True)


def install_artifact_job_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: ArtifactJobService | None,
) -> None:
    @app.get(
        "/api/v1/artifact-jobs/capabilities",
        response_model=ArtifactJobCapabilitiesResponse,
        responses=bounded_error_responses(401, 503),
        operation_id="getArtifactJobCapabilities",
    )
    def capabilities(_actor: Actor = actor_dependency) -> ArtifactJobCapabilitiesResponse:
        return _service(service).capabilities()

    @app.get(
        "/api/v1/recipes/runs/{run_id}/artifact-jobs",
        response_model=ArtifactJobListResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="listArtifactJobsForRun",
    )
    def list_jobs(
        run_id: str = Path(pattern=_UUID), _actor: Actor = actor_dependency
    ) -> ArtifactJobListResponse:
        try:
            return {
                "jobs": [_view(item) for item in _service(service).list_for_run(run_id)]
            }
        except KeyError:
            raise HTTPException(
                status_code=404, detail="recipe run not found"
            ) from None

    @app.post(
        "/api/v1/recipes/runs/{run_id}/artifact-jobs",
        response_model=ArtifactJobResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_201_CREATED,
        operation_id="createArtifactJob",
    )
    def create_job(
        body: ArtifactJobCreate,
        request: Request,
        run_id: str = Path(pattern=_UUID),
        actor: Actor = actor_dependency,
    ) -> ArtifactJobResponse:
        _mutating(actor)
        try:
            return _view(
                _service(service).create(
                    run_id,
                    interface=body.interface,
                    parameters=body.parameters,
                    inputs=[item.model_dump() for item in body.inputs],
                    output_limits=body.output_limits.model_dump(),
                    timeout_seconds=body.timeout_seconds,
                    actor=actor.subject,
                    request_id=request.state.request_id,
                )
            )
        except ArtifactJobError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.put(
        "/api/v1/artifact-jobs/{job_id}/inputs/{name}",
        response_model=ArtifactJobResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="uploadArtifactJobInput",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            },
            "x-vonk-streaming-transport": True,
        },
    )
    async def upload_input(
        request: Request,
        job_id: str = Path(pattern=_UUID),
        name: str = Path(pattern=_NAME),
        content_sha256: Annotated[
            str, Header(alias="X-Content-SHA256", pattern=_DIGEST)
        ] = "",
        content_type: Annotated[
            str, Header(alias="Content-Type", pattern=_MEDIA_TYPE)
        ] = "",
        content_length: Annotated[
            int, Header(alias="Content-Length", ge=0, le=MAX_INPUT_FILE_BYTES)
        ] = 0,
        actor: Actor = actor_dependency,
    ) -> ArtifactJobResponse:
        _mutating(actor)
        try:
            return _view(
                await _service(service).put_input_stream(
                    job_id,
                    name=name,
                    media_type=content_type,
                    expected_sha256=content_sha256,
                    content_length=content_length,
                    chunks=request.stream(),
                )
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact job not found"
            ) from None
        except ArtifactJobError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post(
        "/api/v1/artifact-jobs/{job_id}/finalize",
        response_model=ArtifactJobResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="finalizeArtifactJob",
    )
    def finalize_job(
        job_id: str = Path(pattern=_UUID), actor: Actor = actor_dependency
    ) -> ArtifactJobResponse:
        _mutating(actor)
        try:
            return _view(_service(service).finalize(job_id))
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact job not found"
            ) from None
        except ArtifactJobError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post(
        "/api/v1/artifact-jobs/{job_id}/submit",
        response_model=ArtifactJobResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="submitArtifactJob",
    )
    def submit_job(
        request: Request,
        job_id: str = Path(pattern=_UUID),
        actor: Actor = actor_dependency,
    ) -> ArtifactJobResponse:
        _mutating(actor)
        try:
            return _view(
                _service(service).submit(
                    job_id,
                    actor=actor.subject,
                    request_id=request.state.request_id,
                )
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact job not found"
            ) from None
        except (ArtifactJobError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.get(
        "/api/v1/artifact-jobs/{job_id}",
        response_model=ArtifactJobResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getArtifactJobStatus",
    )
    def job_status(
        job_id: str = Path(pattern=_UUID), _actor: Actor = actor_dependency
    ) -> ArtifactJobResponse:
        try:
            return _view(_service(service).get(job_id))
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact job not found"
            ) from None

    @app.post(
        "/api/v1/artifact-jobs/{job_id}/cancel",
        response_model=ArtifactJobResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="cancelArtifactJob",
    )
    def cancel_job(
        body: CancelRequest,
        request: Request,
        job_id: str = Path(pattern=_UUID),
        actor: Actor = actor_dependency,
    ) -> ArtifactJobResponse:
        _mutating(actor)
        try:
            return _view(
                _service(service).cancel(
                    job_id,
                    actor=actor.subject,
                    request_id=request.state.request_id,
                    reason=body.reason,
                )
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact job not found"
            ) from None
        except ArtifactJobError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.get(
        "/api/v1/artifact-jobs/{job_id}/result",
        response_model=ArtifactJobResponse,
        responses=bounded_error_responses(401, 404, 409, 422, 503),
        operation_id="getArtifactJobResult",
    )
    def result_metadata(
        job_id: str = Path(pattern=_UUID), _actor: Actor = actor_dependency
    ) -> ArtifactJobResponse:
        try:
            return _view(_service(service).result_metadata(job_id))
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact job not found"
            ) from None
        except ArtifactJobError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.get(
        "/api/v1/artifact-jobs/{job_id}/results/{sha256}",
        operation_id="downloadArtifactJobResult",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Artifact result byte stream",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def download_result(
        job_id: str = Path(pattern=_UUID),
        sha256: str = Path(pattern=_DIGEST),
        _actor: Actor = actor_dependency,
    ) -> Response:
        try:
            path, media_type, name, size_bytes = _service(service).result_blob(
                job_id, sha256
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact result not found"
            ) from None
        except ArtifactJobError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return StreamingResponse(
            ArtifactBlobStore.iter_file(path),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Length": str(size_bytes),
                "X-Content-SHA256": sha256,
            },
        )

    @app.get(
        "/agent/v1/recipe-jobs/{job_id}/inputs/{sha256}",
        include_in_schema=False,
    )
    def agent_input(request: Request, job_id: str, sha256: str) -> Response:
        identity = agent_identity_from_scope(request.scope)
        if identity is None:
            raise HTTPException(
                status_code=401, detail="verified agent identity required"
            )
        try:
            path, media_type, size_bytes = _service(service).input_blob(
                job_id, sha256, node_id=identity.node_id
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact input not found"
            ) from None
        except ArtifactJobError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return StreamingResponse(
            ArtifactBlobStore.iter_file(path),
            media_type=media_type,
            headers={
                "Content-Length": str(size_bytes),
                "X-Content-SHA256": sha256,
            },
        )

    @app.put(
        "/agent/v1/recipe-jobs/{job_id}/outputs/{sha256}",
        status_code=status.HTTP_204_NO_CONTENT,
        include_in_schema=False,
    )
    async def agent_output(
        request: Request,
        job_id: str,
        sha256: str,
        artifact_name: Annotated[str, Header(alias="X-Vonk-Artifact-Name")],
        content_type: Annotated[str, Header(alias="Content-Type")],
        content_length: Annotated[
            int, Header(alias="Content-Length", ge=0, le=1024**3)
        ],
    ) -> Response:
        identity = agent_identity_from_scope(request.scope)
        if identity is None:
            raise HTTPException(
                status_code=401, detail="verified agent identity required"
            )
        try:
            await _service(service).put_output_stream(
                job_id,
                node_id=identity.node_id,
                name=artifact_name,
                media_type=content_type,
                expected_sha256=sha256,
                content_length=content_length,
                chunks=request.stream(),
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="artifact job not found"
            ) from None
        except (AgentProtocolError, ArtifactJobError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return Response(status_code=204)


__all__ = ["install_artifact_job_routes"]
