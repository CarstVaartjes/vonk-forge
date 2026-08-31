"""Authenticated one-shot recipe-to-Spark placement routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Request, status

from .audit import AuditRecord
from .auth import MUTATION_ROLES, Actor
from .library_placement_contract import (
    LibraryPlacementApplication,
    LibraryPlacementApplyRequest,
    LibraryPlacementPreview,
    LibraryPlacementPreviewRequest,
)
from .library_placements import LibraryPlacementConflict
from .operation_api import bounded_error_responses

LIBRARY_PLACEMENT_OPERATION_IDS = {
    ("post", "/api/v1/library/placements/preview"): "previewLibraryPlacement",
    ("post", "/api/v1/library/placements"): "applyLibraryPlacement",
    ("get", "/api/v1/library/placements/{placement_id}"): "getLibraryPlacement",
}
_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def install_library_placement_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: Any | None,
    audits: Any,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(LIBRARY_PLACEMENT_OPERATION_IDS)
    authenticated = actor_dependency

    def placements() -> Any:
        if service is None:
            raise HTTPException(status_code=503, detail="Library placement unavailable")
        return service

    def require_mutation(actor: Actor, route: str) -> None:
        if actor.role not in MUTATION_ROLES[("POST", route)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    @app.post(
        "/api/v1/library/placements/preview",
        response_model=LibraryPlacementPreview,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewLibraryPlacement",
    )
    def preview_placement(
        body: LibraryPlacementPreviewRequest,
        actor: Actor = authenticated,
    ) -> LibraryPlacementPreview:
        require_mutation(actor, "/api/v1/library/placements/preview")
        try:
            return placements().preview(body, actor=actor.subject)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Library recipe not found"
            ) from None
        except LibraryPlacementConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Library placement preview unavailable"
            ) from None

    @app.post(
        "/api/v1/library/placements",
        response_model=LibraryPlacementApplication,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="applyLibraryPlacement",
    )
    def apply_placement(
        request: Request,
        body: LibraryPlacementApplyRequest,
        actor: Actor = authenticated,
    ) -> LibraryPlacementApplication:
        require_mutation(actor, "/api/v1/library/placements")
        try:
            result = placements().apply(body, actor=actor.subject)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Library recipe not found"
            ) from None
        except LibraryPlacementConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Library placement apply unavailable"
            ) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "library.placement.apply",
                None,
                (result.id, result.plan_digest, *result.selected_node_ids),
            )
        )
        return result

    @app.get(
        "/api/v1/library/placements/{placement_id}",
        response_model=LibraryPlacementApplication,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getLibraryPlacement",
    )
    def get_placement(
        placement_id: Annotated[str, Path(pattern=_UUID)],
        _actor: Actor = authenticated,
    ) -> LibraryPlacementApplication:
        try:
            return placements().application(placement_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Library placement not found"
            ) from None
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Library placement unavailable"
            ) from None


__all__ = [
    "LIBRARY_PLACEMENT_OPERATION_IDS",
    "install_library_placement_routes",
]
