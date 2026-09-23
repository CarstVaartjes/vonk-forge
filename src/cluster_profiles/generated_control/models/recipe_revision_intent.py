from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="RecipeRevisionIntent")



@_attrs_define
class RecipeRevisionIntent:
    """
        Attributes:
            recipe_revision_id (str):
            build_input_sha256 (Union[None, Unset, str]):
            effective_execution_key (Union[None, Unset, str]):
            force (Union[Unset, bool]):  Default: False.
            force_download (Union[Unset, bool]):  Default: False.
            force_rebuild (Union[Unset, bool]):  Default: False.
            kind (Union[Literal['revision'], Unset]):  Default: 'revision'.
            model_digest (Union[None, Unset, str]):
     """

    recipe_revision_id: str
    build_input_sha256: Union[None, Unset, str] = UNSET
    effective_execution_key: Union[None, Unset, str] = UNSET
    force: Union[Unset, bool] = False
    force_download: Union[Unset, bool] = False
    force_rebuild: Union[Unset, bool] = False
    kind: Union[Literal['revision'], Unset] = 'revision'
    model_digest: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        recipe_revision_id = self.recipe_revision_id

        build_input_sha256: Union[None, Unset, str]
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        effective_execution_key: Union[None, Unset, str]
        if isinstance(self.effective_execution_key, Unset):
            effective_execution_key = UNSET
        else:
            effective_execution_key = self.effective_execution_key

        force = self.force

        force_download = self.force_download

        force_rebuild = self.force_rebuild

        kind = self.kind

        model_digest: Union[None, Unset, str]
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

        def _parse_build_input_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_effective_execution_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        effective_execution_key = _parse_effective_execution_key(d.pop("effective_execution_key", UNSET))


        force = d.pop("force", UNSET)

        force_download = d.pop("force_download", UNSET)

        force_rebuild = d.pop("force_rebuild", UNSET)

        kind = cast(Union[Literal['revision'], Unset] , d.pop("kind", UNSET))
        if kind != 'revision' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'revision', got '{kind}'")

        def _parse_model_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

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
