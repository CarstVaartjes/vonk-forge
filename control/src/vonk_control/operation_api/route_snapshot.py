"""Operation Api: route snapshot."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.route_activation import ActivationMarker

from ..strict_json import read_stored_model


def _stored_activation_marker(value: object) -> ActivationMarker:
    try:
        document = canonical_message(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("durable activation marker is invalid") from error
    try:
        return read_stored_model(ActivationMarker, document, from_json=True)
    except ValidationError as error:
        raise RuntimeError("durable activation marker is invalid") from error


@dataclass(frozen=True)
class _ActiveRouteSnapshot:
    marker: ActivationMarker
    marker_digest: str
    route_digest: str
    litellm_digest: str | None
    bundle_digest: str
    authority_id: str
    owner_generation: int
    publication_generation: int
    plan_digest: str
