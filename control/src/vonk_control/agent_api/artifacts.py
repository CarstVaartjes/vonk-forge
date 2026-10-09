"""Agent api: artifacts concerns."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import ValidationError
from vonk_agent_protocol import (
    DistributionAssignment,
    DistributionCode,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.agent_words import ProgressPhase
from vonk_agent_protocol.http_failure import HttpRefusalReason

from vonk_control.http_errors import SecurityHTTPError

from ..distribution import DistributionError, DistributionUnknown
from ..download_contract import download_responses, upload_request_body
from ..models import RecipeBuild
from ..runtime_image_preparation import IMAGE_CACHE_DIRECTORY
from ..strict_json import _retry_later_response
from .common import (
    _DISTRIBUTION_ERROR_CODE,
    AgentApiServices,
    RecipeImageUploadHeaders,
    RecipeImageUploadStatus,
    _authenticated_identity,
    _commit_recipe_image_upload,
    _flush_and_sync,
    _now,
    _owned_artifact,
    _prepare_recipe_image_upload,
    _range,
    _require_services,
    _scope_identity,
    _served_from_edge,
    _sha256_path,
)


def install_artifacts_routes(
    agent: APIRouter, services: AgentApiServices | None
) -> None:
    def image_upload_context(build_id: str, request: Request):
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        try:
            uuid.UUID(build_id)
            headers = RecipeImageUploadHeaders.model_validate(
                {
                    "layout_sha256": request.headers.get(
                        "x-vonk-oci-layout-sha256", ""
                    ),
                    "image_digest": request.headers.get("x-vonk-image-digest", ""),
                    "image_bytes": request.headers.get("x-vonk-image-bytes", ""),
                    "offset": request.headers.get("x-vonk-upload-offset", "0"),
                }
            )
        except (ValueError, ValidationError):
            raise HTTPException(
                status_code=422, detail="image upload identity is invalid"
            ) from None
        if (
            headers.image_bytes > required.max_recipe_image_bytes
            or headers.offset > headers.image_bytes
        ):
            raise HTTPException(status_code=422, detail="image upload size is invalid")
        with required.sessions() as session:
            build = session.get(RecipeBuild, build_id)
            if (
                build is None
                or build.builder_node_id != identity.node_id
                or build.state != ProgressPhase.BUILDING
            ):
                raise HTTPException(
                    status_code=404, detail="recipe build does not exist"
                )
        key = hashlib.sha256(
            f"{identity.node_id}:{build_id}:{headers.image_digest}:{headers.layout_sha256}:{headers.image_bytes}".encode()
        ).hexdigest()
        return required, identity, headers, key

    upload_header_names = {
        "layout_sha256": "x-vonk-oci-layout-sha256",
        "image_digest": "x-vonk-image-digest",
        "image_bytes": "x-vonk-image-bytes",
        "offset": "x-vonk-upload-offset",
    }

    upload_schema = RecipeImageUploadHeaders.model_json_schema()

    upload_parameters = [
        {
            "in": "header",
            "name": name,
            "required": field in upload_schema["required"],
            "schema": upload_schema["properties"][field],
        }
        for field, name in upload_header_names.items()
    ]

    @agent.head(
        "/recipe-builds/{build_id}/image",
        response_class=Response,
        openapi_extra={"parameters": upload_parameters},
        responses={
            200: {
                "description": "Accepted archive cursor",
                "headers": {
                    name: {
                        "schema": RecipeImageUploadStatus.model_json_schema()[
                            "properties"
                        ][field]
                    }
                    for field, name in {
                        "offset": "x-vonk-upload-offset",
                        "complete": "x-vonk-upload-complete",
                    }.items()
                },
            }
        },
    )
    async def recipe_image_upload_status(build_id: str, request: Request) -> Response:
        required, _, headers, key = image_upload_context(build_id, request)
        with required.sessions() as session:
            build = session.get(RecipeBuild, build_id)
            if build is None:
                raise HTTPException(
                    status_code=404, detail="recipe build does not exist"
                )
            complete = (
                build.image_digest == headers.image_digest
                and build.oci_layout_sha256 == headers.layout_sha256
                and build.image_bytes == headers.image_bytes
            )
        destination = (
            required.artifact_root / IMAGE_CACHE_DIRECTORY / headers.layout_sha256
        )
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError:
            raise UnknownOutcomeError(
                "image cursor storage observation is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from None
        if (
            complete
            and metadata is not None
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_size == headers.image_bytes
        ):
            return RecipeImageUploadStatus(
                offset=headers.image_bytes, complete=True
            ).response()
        descriptor, _temporary = await asyncio.to_thread(
            _prepare_recipe_image_upload,
            required.artifact_root / IMAGE_CACHE_DIRECTORY,
            key,
        )
        try:
            offset = os.fstat(descriptor).st_size
            if offset > headers.image_bytes:
                os.ftruncate(descriptor, 0)
                offset = 0
            return RecipeImageUploadStatus(offset=offset, complete=False).response()
        finally:
            os.close(descriptor)

    @agent.put(
        "/recipe-builds/{build_id}/image",
        status_code=status.HTTP_204_NO_CONTENT,
        openapi_extra=upload_request_body("application/x-tar")
        | {"parameters": upload_parameters},
    )
    async def upload_recipe_image(build_id: str, request: Request) -> Response:
        required, identity, headers, key = image_upload_context(build_id, request)
        media_type = request.headers.get("content-type", "").partition(";")[0].strip()
        if media_type.lower() != "application/x-tar":
            raise HTTPException(
                status_code=415, detail="Docker image archive media type is required"
            )
        try:
            content_length = int(request.headers.get("content-length", ""))
        except ValueError:
            raise HTTPException(
                status_code=411, detail="image length is required"
            ) from None
        if content_length != headers.image_bytes - headers.offset:
            raise HTTPException(
                status_code=422, detail="image upload length does not match cursor"
            )
        descriptor, temporary = await asyncio.to_thread(
            _prepare_recipe_image_upload,
            required.artifact_root / IMAGE_CACHE_DIRECTORY,
            key,
        )
        stream = os.fdopen(descriptor, "r+b")
        try:
            if os.fstat(descriptor).st_size > headers.image_bytes:
                await asyncio.to_thread(stream.truncate, 0)
            if os.fstat(descriptor).st_size != headers.offset:
                raise HTTPException(
                    status_code=409, detail="image upload cursor changed"
                )
            stream.seek(headers.offset)
            received = headers.offset
            async for chunk in request.stream():
                received += len(chunk)
                if received > headers.image_bytes:
                    raise HTTPException(
                        status_code=413, detail="recipe image is too large"
                    )
                await asyncio.to_thread(stream.write, chunk)
            # A truncated/interrupted transfer remains available to the next HEAD/PUT.
            await asyncio.to_thread(_flush_and_sync, stream)
            if received != headers.image_bytes:
                raise HTTPException(
                    status_code=422, detail="image upload is incomplete"
                )
            if (
                await asyncio.to_thread(_sha256_path, temporary, headers.image_bytes)
                != headers.layout_sha256
            ):
                await asyncio.to_thread(stream.truncate, 0)
                raise SecurityHTTPError(
                    reason=HttpRefusalReason.INVALID_DIGEST,
                    status_code=422,
                    detail="recipe image digest changed",
                )
            destination = (
                required.artifact_root / IMAGE_CACHE_DIRECTORY / headers.layout_sha256
            )
            # Content publication holds no SQL session/row lock. Availability
            # is adopted only after the authority is rechecked below; cancelled
            # writers may leave reusable bytes, never a new build effect.
            await asyncio.to_thread(
                _commit_recipe_image_upload,
                temporary,
                destination,
                expected_bytes=headers.image_bytes,
            )
            with required.sessions.begin() as session:
                build = session.get(RecipeBuild, build_id, with_for_update=True)
                if (
                    build is None
                    or build.builder_node_id != identity.node_id
                    or build.state != ProgressPhase.BUILDING
                ):
                    raise HTTPException(
                        status_code=409, detail="recipe build authority changed"
                    )
                build.image_digest = headers.image_digest
                build.oci_layout_sha256 = headers.layout_sha256
                build.image_bytes = headers.image_bytes
                build.updated_at = _now(required.clock())
        except PermissionError:
            raise HTTPException(
                status_code=503, detail="upload storage observation unavailable"
            ) from None
        except OSError:
            raise UnknownOutcomeError(
                "upload storage observation is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from None
        finally:
            await asyncio.to_thread(stream.close)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.get(
        "/artifacts/{sha256}",
        response_class=Response,
        responses=download_responses("application/octet-stream", partial=True),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def artifact(sha256: str, request: Request) -> Response:
        """Authorize one owned artifact; the edge serves its bytes and range."""
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        path, size = _owned_artifact(required, identity, sha256)
        _range(request.headers.get("range"), size, required.max_range_bytes)
        return _served_from_edge(required, path, f'"sha256:{sha256}"')

    def _distribution_error(error: DistributionError) -> HTTPException:
        # Preserve the typed cause and bounded observation retry on the wire.
        headers = (
            {"x-vonk-error-code": error.code}
            if _DISTRIBUTION_ERROR_CODE.fullmatch(error.code)
            else {}
        )
        if isinstance(error, DistributionUnknown):
            headers.update(_retry_later_response(error).headers)
            headers["Cache-Control"] = "no-store"
        if isinstance(error, SecurityRefusalError) or error.code in {
            DistributionCode.UNASSIGNED,
            DistributionCode.WRONG_NODE,
            DistributionCode.EXPIRED,
        }:
            return SecurityHTTPError(
                reason=HttpRefusalReason.AUTHORITY_DENIED,
                status_code=403,
                detail=error.detail,
                headers=headers,
            )
        if error.code == DistributionCode.OBJECT_INVALID:
            return HTTPException(status_code=404, detail=error.detail, headers=headers)
        return HTTPException(status_code=503, detail=error.detail, headers=headers)

    @agent.get(
        "/distribution/manifests/{plan_digest}",
        operation_id="getAgentDistributionManifest",
        response_model=DistributionAssignment,
    )
    def distribution_manifest(
        plan_digest: str, request: Request, response: Response
    ) -> DistributionAssignment:
        """Return the exact model plus OCI object set authorized for this node."""
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if required.distribution is None:
            raise HTTPException(
                status_code=503, detail="agent distribution is unavailable"
            )
        try:
            required.distribution.prepare_request_delivery(
                node_id=identity.node_id, plan_digest=plan_digest
            )
            assignment = required.distribution.authorize(
                node_id=identity.node_id,
                plan_digest=plan_digest,
            )
        except DistributionError as error:
            raise _distribution_error(error) from None
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"plan:{plan_digest}"'
        return assignment.wire()

    @agent.get(
        "/distribution/objects/{sha256}",
        operation_id="downloadAgentDistributionObject",
        response_class=Response,
        responses=download_responses("application/octet-stream", partial=True),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def distribution_object(sha256: str, request: Request) -> Response:
        """Authorize one assigned immutable object; the edge serves its range."""
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if required.distribution is None:
            raise HTTPException(
                status_code=503, detail="agent distribution is unavailable"
            )
        plan_digest = request.query_params.get("plan_digest")
        if plan_digest is None:
            raise SecurityHTTPError(
                reason=HttpRefusalReason.AUTHORITY_DENIED,
                status_code=403,
                detail="assignment is required",
            )
        try:
            _assignment, object_spec, stored = required.distribution.locate_object(
                node_id=identity.node_id,
                plan_digest=plan_digest,
                digest=sha256,
            )
        except DistributionError as error:
            raise _distribution_error(error) from None
        # Only the name and size are needed; the edge opens the file itself.
        etag = f'"sha256:{object_spec.sha256}"'
        # The edge answers the client's range from the file, so a checkpoint
        # from another object must be refused here rather than served.
        if request.headers.get("if-range") not in {
            None,
            etag,
            f"sha256:{object_spec.sha256}",
        }:
            raise HTTPException(status_code=412, detail="object checkpoint changed")
        _range(request.headers.get("range"), stored.size, required.max_range_bytes)
        return _served_from_edge(required, stored.path, etag)
