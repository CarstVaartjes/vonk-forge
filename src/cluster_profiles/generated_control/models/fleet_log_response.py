from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_log_entry import FleetLogEntry





T = TypeVar("T", bound="FleetLogResponse")



@_attrs_define
class FleetLogResponse:
    """
        Attributes:
            entries (list['FleetLogEntry']):
            follow (bool):
            lines (int):
            node_id (str):
            retained (bool):
            since (Union[None, datetime.datetime]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    entries: list['FleetLogEntry']
    follow: bool
    lines: int
    node_id: str
    retained: bool
    since: Union[None, datetime.datetime]
    schema_version: Union[Literal[2], Unset] = 2
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_log_entry import FleetLogEntry
        entries = []
        for entries_item_data in self.entries:
            entries_item = entries_item_data.to_dict()
            entries.append(entries_item)



        follow = self.follow

        lines = self.lines

        node_id = self.node_id

        retained = self.retained

        since: Union[None, str]
        if isinstance(self.since, datetime.datetime):
            since = self.since.isoformat()
        else:
            since = self.since

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "entries": entries,
            "follow": follow,
            "lines": lines,
            "node_id": node_id,
            "retained": retained,
            "since": since,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_log_entry import FleetLogEntry
        d = dict(src_dict)
        entries = []
        _entries = d.pop("entries")
        for entries_item_data in (_entries):
            entries_item = FleetLogEntry.from_dict(entries_item_data)



            entries.append(entries_item)


        follow = d.pop("follow")

        lines = d.pop("lines")

        node_id = d.pop("node_id")

        retained = d.pop("retained")

        def _parse_since(data: object) -> Union[None, datetime.datetime]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                since_type_0 = isoparse(data)



                return since_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, datetime.datetime], data)

        since = _parse_since(d.pop("since"))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        fleet_log_response = cls(
            entries=entries,
            follow=follow,
            lines=lines,
            node_id=node_id,
            retained=retained,
            since=since,
            schema_version=schema_version,
        )


        fleet_log_response.additional_properties = d
        return fleet_log_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
