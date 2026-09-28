from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="EndpointResponse")



@_attrs_define
class EndpointResponse:
    """ One published alias: clients use `api_base` with `alias` as the model.

    `backend_api_base` is the Spark-local serving address LiteLLM routes to.
    It is diagnostic only and usually unreachable from a client.

        Attributes:
            alias (str):
            api_base (str):
            backend_api_base (str):
            expires_at (str):
            generation (int):
            node_id (str):
            observed_at (str):
            plan_digest (str):
     """

    alias: str
    api_base: str
    backend_api_base: str
    expires_at: str
    generation: int
    node_id: str
    observed_at: str
    plan_digest: str





    def to_dict(self) -> dict[str, Any]:
        alias = self.alias

        api_base = self.api_base

        backend_api_base = self.backend_api_base

        expires_at = self.expires_at

        generation = self.generation

        node_id = self.node_id

        observed_at = self.observed_at

        plan_digest = self.plan_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "api_base": api_base,
            "backend_api_base": backend_api_base,
            "expires_at": expires_at,
            "generation": generation,
            "node_id": node_id,
            "observed_at": observed_at,
            "plan_digest": plan_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        alias = d.pop("alias")

        api_base = d.pop("api_base")

        backend_api_base = d.pop("backend_api_base")

        expires_at = d.pop("expires_at")

        generation = d.pop("generation")

        node_id = d.pop("node_id")

        observed_at = d.pop("observed_at")

        plan_digest = d.pop("plan_digest")

        endpoint_response = cls(
            alias=alias,
            api_base=api_base,
            backend_api_base=backend_api_base,
            expires_at=expires_at,
            generation=generation,
            node_id=node_id,
            observed_at=observed_at,
            plan_digest=plan_digest,
        )

        return endpoint_response
