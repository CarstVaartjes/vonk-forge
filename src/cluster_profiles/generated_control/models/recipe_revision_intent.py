from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RecipeRevisionIntent")



@_attrs_define
class RecipeRevisionIntent:
    """
        Attributes:
            recipe_revision_id (str):
            build_input_sha256 (None | str | Unset):
            effective_execution_key (None | str | Unset):
            force (bool | Unset):  Default: False.
            force_download (bool | Unset):  Default: False.
            force_rebuild (bool | Unset):  Default: False.
            kind (Literal['revision'] | Unset):  Default: 'revision'.
            model_digest (None | str | Unset):
     """

    recipe_revision_id: str
    build_input_sha256: None | str | Unset = UNSET
    effective_execution_key: None | str | Unset = UNSET
    force: bool | Unset = False
    force_download: bool | Unset = False
    force_rebuild: bool | Unset = False
    kind: Literal['revision'] | Unset = 'revision'
    model_digest: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        recipe_revision_id = self.recipe_revision_id

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        effective_execution_key: None | str | Unset
        if isinstance(self.effective_execution_key, Unset):
            effective_execution_key = UNSET
        else:
            effective_execution_key = self.effective_execution_key

        force = self.force

        force_download = self.force_download

        force_rebuild = self.force_rebuild

        kind = self.kind

        model_digest: None | str | Unset
        if isinstance(self.model_digest, Unset):
            model_digest = UNSET
        else:
            model_digest = self.model_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "recipe_revision_id": recipe_revision_id,
        })
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if effective_execution_key is not UNSET:
            field_dict["effective_execution_key"] = effective_execution_key
        if force is not UNSET:
            field_dict["force"] = force
        if force_download is not UNSET:
            field_dict["force_download"] = force_download
        if force_rebuild is not UNSET:
            field_dict["force_rebuild"] = force_rebuild
        if kind is not UNSET:
            field_dict["kind"] = kind
        if model_digest is not UNSET:
            field_dict["model_digest"] = model_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        recipe_revision_id = d.pop("recipe_revision_id")

        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_effective_execution_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effective_execution_key = _parse_effective_execution_key(d.pop("effective_execution_key", UNSET))


        force = d.pop("force", UNSET)

        force_download = d.pop("force_download", UNSET)

        force_rebuild = d.pop("force_rebuild", UNSET)

        kind = cast(Literal['revision'] | Unset , d.pop("kind", UNSET))
        if kind != 'revision' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'revision', got '{kind}'")

        def _parse_model_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_digest = _parse_model_digest(d.pop("model_digest", UNSET))


        recipe_revision_intent = cls(
            recipe_revision_id=recipe_revision_id,
            build_input_sha256=build_input_sha256,
            effective_execution_key=effective_execution_key,
            force=force,
            force_download=force_download,
            force_rebuild=force_rebuild,
            kind=kind,
            model_digest=model_digest,
        )

        return recipe_revision_intent
