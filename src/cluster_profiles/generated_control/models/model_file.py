from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.model_file_part import ModelFilePart





T = TypeVar("T", bound="ModelFile")



@_attrs_define
class ModelFile:
    """ One entry in the complete immutable model file manifest.

    ``sha256`` and ``size_bytes`` always describe the whole installed file.
    When the source publishes the file only as split parts, ``parts`` lists
    them in joining order (byte concatenation yields the file). Omitted means
    the source publishes the file whole, so the same bytes have the same
    identity whether the source splits them or not.

        Attributes:
            id (str):
            path (str):
            roles (list[str]):
            sha256 (str):
            size_bytes (int):
            parts (list[ModelFilePart] | None | Unset):
     """

    id: str
    path: str
    roles: list[str]
    sha256: str
    size_bytes: int
    parts: list[ModelFilePart] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_file_part import ModelFilePart # noqa: PLC0415
        id = self.id

        path = self.path

        roles = self.roles



        sha256 = self.sha256

        size_bytes = self.size_bytes

        parts: list[dict[str, Any]] | None | Unset
        if isinstance(self.parts, Unset):
            parts = UNSET
        elif isinstance(self.parts, list):
            parts = []
            for parts_type_0_item_data in self.parts:
                parts_type_0_item = parts_type_0_item_data.to_dict()
                parts.append(parts_type_0_item)


        else:
            parts = self.parts


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "path": path,
            "roles": roles,
            "sha256": sha256,
            "size_bytes": size_bytes,
        })
        if parts is not UNSET:
            field_dict["parts"] = parts

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_file_part import ModelFilePart # noqa: PLC0415
        d = dict(src_dict)
        id = d.pop("id")

        path = d.pop("path")

        roles = cast(list[str], d.pop("roles"))


        sha256 = d.pop("sha256")

        size_bytes = d.pop("size_bytes")

        def _parse_parts(data: object) -> list[ModelFilePart] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                parts_type_0 = []
                _parts_type_0 = data
                for parts_type_0_item_data in (_parts_type_0):
                    parts_type_0_item = ModelFilePart.from_dict(parts_type_0_item_data)



                    parts_type_0.append(parts_type_0_item)

                return parts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ModelFilePart] | None | Unset, data)

        parts = _parse_parts(d.pop("parts", UNSET))


        model_file = cls(
            id=id,
            path=path,
            roles=roles,
            sha256=sha256,
            size_bytes=size_bytes,
            parts=parts,
        )

        return model_file
