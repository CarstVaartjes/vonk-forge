"""Authenticated JSON download for one failed operation attempt."""

import re

from fastapi import HTTPException, Query, Response

from .bounded_json import BoundedJSONError
from .failure_evidence import FailureEvidenceBundle, FailureEvidenceService
from .integer_domains import MAX_DATABASE_INTEGER
from .operation_api import bounded_error_responses


def install_failure_evidence_routes(
    app, *, actor_dependency, service: FailureEvidenceService | None
):
    @app.get(
        "/api/operations/{operation_id}/evidence",
        response_model=FailureEvidenceBundle,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getOperationEvidence",
    )
    def evidence(
        operation_id: str,
        attempt: int = Query(ge=0, le=MAX_DATABASE_INTEGER),
        _actor=actor_dependency,
    ):
        if service is None:
            raise HTTPException(503, "failure evidence service unavailable")
        try:
            bundle = service.read(operation_id, attempt)
        except KeyError:
            raise HTTPException(
                404, "failure evidence unavailable for this attempt"
            ) from None
        except (BoundedJSONError, ValueError, OSError):
            raise HTTPException(503, "failure evidence unavailable") from None
        name = re.sub(r"[^A-Za-z0-9_-]", "_", operation_id)[:64]
        return Response(
            content=bundle.model_dump_json(),
            media_type="application/json",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": (
                    f'attachment; filename="failure-evidence-{name}-{attempt}.json"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
