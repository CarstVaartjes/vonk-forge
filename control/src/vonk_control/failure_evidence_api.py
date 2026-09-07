"""Authenticated stable JSON download for one durable operation attempt."""

from fastapi import HTTPException, Query, Response

from .failure_evidence import FailureEvidenceBundle, FailureEvidenceService
from .operation_api import bounded_error_responses


def install_failure_evidence_routes(
    app, *, actor_dependency, service: FailureEvidenceService
):
    @app.get(
        "/api/v1/operations/{operation_id}/evidence",
        response_model=FailureEvidenceBundle,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getOperationEvidence",
    )
    def evidence(
        operation_id: str, attempt: int = Query(ge=0), _actor=actor_dependency
    ):
        try:
            content, digest, _ = service.read(operation_id, attempt)
        except KeyError:
            raise HTTPException(
                404, "failure evidence unavailable for this attempt"
            ) from None
        except (ValueError, OSError):
            raise HTTPException(503, "failure evidence validation failed") from None
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "ETag": f'"sha256:{digest}"',
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="failure-evidence-{digest[:16]}.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )
