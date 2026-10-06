from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.activation_marker_state import ActivationMarkerState
from ..models.activation_marker_state import check_activation_marker_state
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="ActivationMarker")



@_attrs_define
class ActivationMarker:
    """
        Attributes:
            authority_id (str):
            directory (str):
            evidence_set_digest (str):
            generation (int):
            litellm_sha256 (str):
            manifest_sha256 (str):
            plan_digest (str):
            routes_sha256 (str):
            schema_version (Literal[2]):
            state (ActivationMarkerState):
     """

    authority_id: str
    directory: str
    evidence_set_digest: str
    generation: int
    litellm_sha256: str
    manifest_sha256: str
    plan_digest: str
    routes_sha256: str
    schema_version: Literal[2]
    state: ActivationMarkerState





    def to_dict(self) -> dict[str, Any]:
        authority_id = self.authority_id

        directory = self.directory

        evidence_set_digest = self.evidence_set_digest

        generation = self.generation

        litellm_sha256 = self.litellm_sha256

        manifest_sha256 = self.manifest_sha256

        plan_digest = self.plan_digest

        routes_sha256 = self.routes_sha256

        schema_version = self.schema_version

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_id": authority_id,
            "directory": directory,
            "evidence_set_digest": evidence_set_digest,
            "generation": generation,
            "litellm_sha256": litellm_sha256,
            "manifest_sha256": manifest_sha256,
            "plan_digest": plan_digest,
            "routes_sha256": routes_sha256,
            "schema_version": schema_version,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        authority_id = d.pop("authority_id")

        directory = d.pop("directory")

        evidence_set_digest = d.pop("evidence_set_digest")

        generation = d.pop("generation")

        litellm_sha256 = d.pop("litellm_sha256")

        manifest_sha256 = d.pop("manifest_sha256")

        plan_digest = d.pop("plan_digest")

        routes_sha256 = d.pop("routes_sha256")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        state = check_activation_marker_state(d.pop("state"))




        activation_marker = cls(
            authority_id=authority_id,
            directory=directory,
            evidence_set_digest=evidence_set_digest,
            generation=generation,
            litellm_sha256=litellm_sha256,
            manifest_sha256=manifest_sha256,
            plan_digest=plan_digest,
            routes_sha256=routes_sha256,
            schema_version=schema_version,
            state=state,
        )

        return activation_marker
