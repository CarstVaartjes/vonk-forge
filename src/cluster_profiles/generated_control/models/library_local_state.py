from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_local_state_controller import check_library_local_state_controller
from ..models.library_local_state_controller import LibraryLocalStateController
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.library_local_progress import LibraryLocalProgress





T = TypeVar("T", bound="LibraryLocalState")



@_attrs_define
class LibraryLocalState:
    """ Controller cache and Spark-local runtime evidence kept separate.

        Attributes:
            controller (LibraryLocalStateController):
            preparation (Union['LibraryLocalProgress', None, Unset]):
            running_on (Union[Unset, list[str]]):
     """

    controller: LibraryLocalStateController
    preparation: Union['LibraryLocalProgress', None, Unset] = UNSET
    running_on: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_local_progress import LibraryLocalProgress
        controller: str = self.controller

        preparation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.preparation, Unset):
            preparation = UNSET
        elif isinstance(self.preparation, LibraryLocalProgress):
            preparation = self.preparation.to_dict()
        else:
            preparation = self.preparation

        running_on: Union[Unset, list[str]] = UNSET
        if not isinstance(self.running_on, Unset):
            running_on = self.running_on




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "controller": controller,
        })
        if preparation is not UNSET:
            field_dict["preparation"] = preparation
        if running_on is not UNSET:
            field_dict["running_on"] = running_on

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_local_progress import LibraryLocalProgress
        d = dict(src_dict)
        controller = check_library_local_state_controller(d.pop("controller"))




        def _parse_preparation(data: object) -> Union['LibraryLocalProgress', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preparation_type_0 = LibraryLocalProgress.from_dict(data)



                return preparation_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryLocalProgress', None, Unset], data)

        preparation = _parse_preparation(d.pop("preparation", UNSET))


        running_on = cast(list[str], d.pop("running_on", UNSET))


        library_local_state = cls(
            controller=controller,
            preparation=preparation,
            running_on=running_on,
        )

        return library_local_state
