"""Operator projection api: errors."""

from __future__ import annotations

from fastapi import HTTPException
from vonk_agent_protocol import (
    ControllerErrorCode,
    SecurityRefusalReason,
)

from ..agent_upgrades import AgentUpgradeConflict
from ..api_availability import AVAILABILITY_ERRORS
from ..auth import MUTATION_ROLES, Actor, CursorError
from ..enrollment import (
    EnrollmentDenied,
    RemoteRevocationUncertain,
)
from ..fleet_projection import (
    FleetNode,
    FleetSnapshot,
)
from ..library_projection import LibrarySelectorAmbiguous
from ..logging import redact_text
from ..request_fault import RequestFault


def _node(snapshot: FleetSnapshot, selector: str) -> FleetNode:
    wanted = selector.casefold()
    exact = [value for value in snapshot.nodes if value.id.casefold() == wanted]
    matches = exact or [
        value
        for value in snapshot.nodes
        if wanted
        in {
            value.display_name.casefold(),
            value.hostname.casefold(),
        }
    ]
    if not matches:
        raise KeyError(selector)
    if len(matches) > 1:
        raise LibrarySelectorAmbiguous(selector, sorted(value.id for value in matches))
    return matches[0]


def _domain_refusal_detail(error: Exception) -> str:
    """Bound and redact a domain authority's own refusal text.

    The refusal is built by the owning domain from policy copy, so the boundary
    only has to keep it bounded and redacted; it never carries a stored document.
    """

    return redact_text(str(error))[:256]


def _operator_error(error: Exception) -> HTTPException:
    if isinstance(error, LibrarySelectorAmbiguous):
        from ..library_api import SelectorAmbiguityHTTPError

        return SelectorAmbiguityHTTPError(error)
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="operator object not found")
    if isinstance(error, (CursorError, RequestFault)):
        # Only an explicit request fault blames the caller. Stored-document
        # faults reach the central typed 500 boundary.
        return HTTPException(status_code=422, detail=str(error)[:256])
    if isinstance(error, AgentUpgradeConflict):
        # The upgrade authority refused the plan. That is a conflict between the
        # request and current Fleet state, not a projection fault, so name the
        # layer and keep the authority's bounded reason instead of the generic
        # "operator projection unavailable" tail.
        return HTTPException(
            status_code=409,
            detail=_domain_refusal_detail(error),
            headers={"x-vonk-error-code": ControllerErrorCode.FLEET_UPGRADE_CONFLICT},
        )
    if isinstance(error, RemoteRevocationUncertain):
        # Local revocation is durable; only the CA confirmation is pending. Stay
        # retryable but name the uncertain authority rather than the projection.
        return HTTPException(
            status_code=503,
            detail=_domain_refusal_detail(error),
            headers={
                "x-vonk-error-code": ControllerErrorCode.FLEET_REVOCATION_UNCERTAIN
            },
        )
    if isinstance(error, EnrollmentDenied):
        return HTTPException(
            status_code=409,
            detail=_domain_refusal_detail(error),
            headers={
                "x-vonk-error-code": SecurityRefusalReason.CONTROLLER_FLEET_ENROLLMENT_DENIED.value
            },
        )
    if isinstance(error, AVAILABILITY_ERRORS):
        return HTTPException(status_code=503, detail="operator projection unavailable")
    raise error


def _require_mutation(actor: Actor, method: str, route: str) -> None:
    """Use the shared role table for both cookie and bearer actors."""

    allowed = MUTATION_ROLES.get((method, route))
    if allowed is None or actor.role not in allowed:
        raise HTTPException(status_code=403, detail="insufficient role")
