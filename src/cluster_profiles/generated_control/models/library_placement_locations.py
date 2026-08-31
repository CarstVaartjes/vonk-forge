from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="LibraryPlacementLocations")



@_attrs_define
class LibraryPlacementLocations:
    """
        Attributes:
            installation_ids (list[str]):
            installed (bool):
            run_ids (list[str]):
            running (bool):
     """

    installation_ids: list[str]
    installed: bool
    run_ids: list[str]
    running: bool





    def to_dict(self) -> dict[str, Any]:
        installation_ids = self.installation_ids



        installed = self.installed

        run_ids = self.run_ids



        running = self.running


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_ids": installation_ids,
            "installed": installed,
            "run_ids": run_ids,
            "running": running,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_ids = cast(list[str], d.pop("installation_ids"))


        installed = d.pop("installed")

        run_ids = cast(list[str], d.pop("run_ids"))


        running = d.pop("running")

        library_placement_locations = cls(
            installation_ids=installation_ids,
            installed=installed,
            run_ids=run_ids,
            running=running,
        )

        return library_placement_locations
