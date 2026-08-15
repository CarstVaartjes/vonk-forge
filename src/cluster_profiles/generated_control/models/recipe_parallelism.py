from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeParallelism")



@_attrs_define
class RecipeParallelism:
    """
        Attributes:
            backend (str):
            data (int):
            pipeline (int):
            tensor (int):
     """

    backend: str
    data: int
    pipeline: int
    tensor: int





    def to_dict(self) -> dict[str, Any]:
        backend = self.backend

        data = self.data

        pipeline = self.pipeline

        tensor = self.tensor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "backend": backend,
            "data": data,
            "pipeline": pipeline,
            "tensor": tensor,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        backend = d.pop("backend")

        data = d.pop("data")

        pipeline = d.pop("pipeline")

        tensor = d.pop("tensor")

        recipe_parallelism = cls(
            backend=backend,
            data=data,
            pipeline=pipeline,
            tensor=tensor,
        )

        return recipe_parallelism
