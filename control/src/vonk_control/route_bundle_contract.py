"""The documents of one published route bundle and of its identity.

A route bundle is staged as ``routes.json`` (the aliases the gateway serves and
the endpoint behind each) beside ``litellm.json`` (see
:mod:`vonk_control.litellm`).  The route identity is the document whose digest
names a candidate bundle: the same runs and ranks always produce the same
digest, so an otherwise identical heartbeat never generates a new bundle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator
from vonk_agent_protocol.state_machines import GatewayRouteState

from .integer_domains import MAX_DATABASE_BIGINT
from .strict_json import StrictModel

Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class RouteEndpointDocument(StrictModel):
    """Where one published alias is served: one run's endpoint on one Spark."""

    address: str = Field(min_length=1, max_length=253)
    node_id: str = Field(min_length=1, max_length=128)
    observed_at: str
    operation_id: str = Field(min_length=1, max_length=256)
    path: str = Field(pattern=r"^/")
    port: int = Field(ge=1, le=65535)
    scheme: Literal["http", "https"]

    @field_validator("observed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value


class RouteBundleDocument(StrictModel):
    """``routes.json``: the gateway's published aliases at one generation."""

    generation: int = Field(ge=1)
    routes: dict[Identifier, RouteEndpointDocument]
    schema_version: Literal[2]
    state: GatewayRouteState
    reason: str | None = Field(default=None, max_length=256)


class RouteRankIdentity(StrictModel):
    node_id: str = Field(min_length=1, max_length=128)
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)


class RouteRunIdentity(StrictModel):
    run_id: str = Field(min_length=1, max_length=128)
    alias: str = Field(min_length=1, max_length=128)
    plan_digest: str = Field(min_length=1, max_length=128)
    run_generation: int = Field(le=MAX_DATABASE_BIGINT, ge=0)
    upstream_model: str = Field(min_length=1, max_length=256)
    ranks: list[RouteRankIdentity]


class RouteAcceptedModelPolicy(StrictModel):
    requests_per_minute: int = Field(ge=1, le=100_000)
    tokens_per_minute: int = Field(ge=1, le=100_000_000)
    upstream_model: str = Field(min_length=1, max_length=256)


class RouteAcceptedRunIdentity(StrictModel):
    """Retained immutable serving facts while current bookkeeping is unreadable."""

    run_id: str = Field(min_length=1, max_length=128)
    alias: str = Field(min_length=1, max_length=128)
    accepted_endpoint: RouteEndpointDocument
    accepted_policy: RouteAcceptedModelPolicy


class RouteIdentityDocument(StrictModel):
    """The document whose digest names one candidate route bundle."""

    schema_version: Literal[1] = 1
    runs: list[RouteRunIdentity | RouteAcceptedRunIdentity]
    aliases: dict[Identifier, str]
