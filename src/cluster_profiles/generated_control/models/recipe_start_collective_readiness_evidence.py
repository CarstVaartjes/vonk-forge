from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Literal, cast
from typing import Union
from uuid import UUID






T = TypeVar("T", bound="RecipeStartCollectiveReadinessEvidence")



@_attrs_define
class RecipeStartCollectiveReadinessEvidence:
    """
        Attributes:
            artifact_set_digest (str):
            endpoint (str):
            evidence_digest (str):
            image_digest (str):
            local_address (Union[None, str]):
            master_address (Union[None, str]):
            memory_reservation_bytes (int):
            phase (Literal['collective-readiness']):
            rank (int):
            ready (bool):
            recipe_content_sha256 (str):
            recipe_revision_id (UUID):
            role (str):
            run_generation (int):
            run_id (UUID):
            runtime_arguments_sha256 (str):
            world_size (int):
            master_port (Union[None, Unset, int]):
            model_identity (Union[None, Unset, str]):
     """

    artifact_set_digest: str
    endpoint: str
    evidence_digest: str
    image_digest: str
    local_address: Union[None, str]
    master_address: Union[None, str]
    memory_reservation_bytes: int
    phase: Literal['collective-readiness']
    rank: int
    ready: bool
    recipe_content_sha256: str
    recipe_revision_id: UUID
    role: str
    run_generation: int
    run_id: UUID
    runtime_arguments_sha256: str
    world_size: int
    master_port: Union[None, Unset, int] = UNSET
    model_identity: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_set_digest = self.artifact_set_digest

        endpoint = self.endpoint

        evidence_digest = self.evidence_digest

        image_digest = self.image_digest

        local_address: Union[None, str]
        local_address = self.local_address

        master_address: Union[None, str]
        master_address = self.master_address

        memory_reservation_bytes = self.memory_reservation_bytes

        phase = self.phase

        rank = self.rank

        ready = self.ready

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = str(self.recipe_revision_id)

        role = self.role

        run_generation = self.run_generation

        run_id = str(self.run_id)

        runtime_arguments_sha256 = self.runtime_arguments_sha256

        world_size = self.world_size

        master_port: Union[None, Unset, int]
        if isinstance(self.master_port, Unset):
            master_port = UNSET
        else:
            master_port = self.master_port

        model_identity: Union[None, Unset, str]
        if isinstance(self.model_identity, Unset):
            model_identity = UNSET
        else:
            model_identity = self.model_identity


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_digest": artifact_set_digest,
            "endpoint": endpoint,
            "evidence_digest": evidence_digest,
            "image_digest": image_digest,
            "local_address": local_address,
            "master_address": master_address,
            "memory_reservation_bytes": memory_reservation_bytes,
            "phase": phase,
            "rank": rank,
            "ready": ready,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "role": role,
            "run_generation": run_generation,
            "run_id": run_id,
            "runtime_arguments_sha256": runtime_arguments_sha256,
            "world_size": world_size,
        })
        if master_port is not UNSET:
            field_dict["master_port"] = master_port
        if model_identity is not UNSET:
            field_dict["model_identity"] = model_identity

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_set_digest = d.pop("artifact_set_digest")

        endpoint = d.pop("endpoint")

        evidence_digest = d.pop("evidence_digest")

        image_digest = d.pop("image_digest")

        def _parse_local_address(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        local_address = _parse_local_address(d.pop("local_address"))


        def _parse_master_address(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        master_address = _parse_master_address(d.pop("master_address"))


        memory_reservation_bytes = d.pop("memory_reservation_bytes")

        phase = cast(Literal['collective-readiness'] , d.pop("phase"))
        if phase != 'collective-readiness':
            raise ValueError(f"phase must match const 'collective-readiness', got '{phase}'")

        rank = d.pop("rank")

        ready = d.pop("ready")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = UUID(d.pop("recipe_revision_id"))




        role = d.pop("role")

        run_generation = d.pop("run_generation")

        run_id = UUID(d.pop("run_id"))




        runtime_arguments_sha256 = d.pop("runtime_arguments_sha256")

        world_size = d.pop("world_size")

        def _parse_master_port(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        master_port = _parse_master_port(d.pop("master_port", UNSET))


        def _parse_model_identity(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_identity = _parse_model_identity(d.pop("model_identity", UNSET))


        recipe_start_collective_readiness_evidence = cls(
            artifact_set_digest=artifact_set_digest,
            endpoint=endpoint,
            evidence_digest=evidence_digest,
            image_digest=image_digest,
            local_address=local_address,
            master_address=master_address,
            memory_reservation_bytes=memory_reservation_bytes,
            phase=phase,
            rank=rank,
            ready=ready,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            role=role,
            run_generation=run_generation,
            run_id=run_id,
            runtime_arguments_sha256=runtime_arguments_sha256,
            world_size=world_size,
            master_port=master_port,
            model_identity=model_identity,
        )

        return recipe_start_collective_readiness_evidence
