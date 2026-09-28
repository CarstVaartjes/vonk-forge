from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
from uuid import UUID






T = TypeVar("T", bound="RecipeStartRankLaunchEvidence")



@_attrs_define
class RecipeStartRankLaunchEvidence:
    """
        Attributes:
            artifact_set_digest (str):
            fabric_projection_bound (bool):
            image_digest (str):
            launched (bool):
            local_address (None | str):
            master_address (None | str):
            memory_reservation_bytes (int):
            phase (Literal['rank-launch']):
            process_running (bool):
            rank (int):
            recipe_content_sha256 (str):
            recipe_revision_id (UUID):
            role (str):
            run_generation (int):
            run_id (UUID):
            runtime_arguments_sha256 (str):
            world_size (int):
            master_port (int | None | Unset):
            model_identity (None | str | Unset):
     """

    artifact_set_digest: str
    fabric_projection_bound: bool
    image_digest: str
    launched: bool
    local_address: None | str
    master_address: None | str
    memory_reservation_bytes: int
    phase: Literal['rank-launch']
    process_running: bool
    rank: int
    recipe_content_sha256: str
    recipe_revision_id: UUID
    role: str
    run_generation: int
    run_id: UUID
    runtime_arguments_sha256: str
    world_size: int
    master_port: int | None | Unset = UNSET
    model_identity: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_set_digest = self.artifact_set_digest

        fabric_projection_bound = self.fabric_projection_bound

        image_digest = self.image_digest

        launched = self.launched

        local_address: None | str
        local_address = self.local_address

        master_address: None | str
        master_address = self.master_address

        memory_reservation_bytes = self.memory_reservation_bytes

        phase = self.phase

        process_running = self.process_running

        rank = self.rank

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = str(self.recipe_revision_id)

        role = self.role

        run_generation = self.run_generation

        run_id = str(self.run_id)

        runtime_arguments_sha256 = self.runtime_arguments_sha256

        world_size = self.world_size

        master_port: int | None | Unset
        if isinstance(self.master_port, Unset):
            master_port = UNSET
        else:
            master_port = self.master_port

        model_identity: None | str | Unset
        if isinstance(self.model_identity, Unset):
            model_identity = UNSET
        else:
            model_identity = self.model_identity


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_digest": artifact_set_digest,
            "fabric_projection_bound": fabric_projection_bound,
            "image_digest": image_digest,
            "launched": launched,
            "local_address": local_address,
            "master_address": master_address,
            "memory_reservation_bytes": memory_reservation_bytes,
            "phase": phase,
            "process_running": process_running,
            "rank": rank,
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

        fabric_projection_bound = d.pop("fabric_projection_bound")

        image_digest = d.pop("image_digest")

        launched = d.pop("launched")

        def _parse_local_address(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        local_address = _parse_local_address(d.pop("local_address"))


        def _parse_master_address(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        master_address = _parse_master_address(d.pop("master_address"))


        memory_reservation_bytes = d.pop("memory_reservation_bytes")

        phase = cast(Literal['rank-launch'] , d.pop("phase"))
        if phase != 'rank-launch':
            raise ValueError(f"phase must match const 'rank-launch', got '{phase}'")

        process_running = d.pop("process_running")

        rank = d.pop("rank")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = UUID(d.pop("recipe_revision_id"))




        role = d.pop("role")

        run_generation = d.pop("run_generation")

        run_id = UUID(d.pop("run_id"))




        runtime_arguments_sha256 = d.pop("runtime_arguments_sha256")

        world_size = d.pop("world_size")

        def _parse_master_port(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        master_port = _parse_master_port(d.pop("master_port", UNSET))


        def _parse_model_identity(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_identity = _parse_model_identity(d.pop("model_identity", UNSET))


        recipe_start_rank_launch_evidence = cls(
            artifact_set_digest=artifact_set_digest,
            fabric_projection_bound=fabric_projection_bound,
            image_digest=image_digest,
            launched=launched,
            local_address=local_address,
            master_address=master_address,
            memory_reservation_bytes=memory_reservation_bytes,
            phase=phase,
            process_running=process_running,
            rank=rank,
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

        return recipe_start_rank_launch_evidence
