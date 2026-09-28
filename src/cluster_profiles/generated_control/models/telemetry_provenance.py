from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="TelemetryProvenance")



@_attrs_define
class TelemetryProvenance:
    """
        Attributes:
            collector (str):
            collector_version (str):
            host_uptime_seconds (int | None | Unset):
            source_observed_at (datetime.datetime | None | Unset):
     """

    collector: str
    collector_version: str
    host_uptime_seconds: int | None | Unset = UNSET
    source_observed_at: datetime.datetime | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        collector = self.collector

        collector_version = self.collector_version

        host_uptime_seconds: int | None | Unset
        if isinstance(self.host_uptime_seconds, Unset):
            host_uptime_seconds = UNSET
        else:
            host_uptime_seconds = self.host_uptime_seconds

        source_observed_at: None | str | Unset
        if isinstance(self.source_observed_at, Unset):
            source_observed_at = UNSET
        elif isinstance(self.source_observed_at, datetime.datetime):
            source_observed_at = self.source_observed_at.isoformat()
        else:
            source_observed_at = self.source_observed_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "collector": collector,
            "collector_version": collector_version,
        })
        if host_uptime_seconds is not UNSET:
            field_dict["host_uptime_seconds"] = host_uptime_seconds
        if source_observed_at is not UNSET:
            field_dict["source_observed_at"] = source_observed_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        collector = d.pop("collector")

        collector_version = d.pop("collector_version")

        def _parse_host_uptime_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        host_uptime_seconds = _parse_host_uptime_seconds(d.pop("host_uptime_seconds", UNSET))


        def _parse_source_observed_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                source_observed_at_type_0 = datetime.datetime.fromisoformat(data)



                return source_observed_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        source_observed_at = _parse_source_observed_at(d.pop("source_observed_at", UNSET))


        telemetry_provenance = cls(
            collector=collector,
            collector_version=collector_version,
            host_uptime_seconds=host_uptime_seconds,
            source_observed_at=source_observed_at,
        )

        return telemetry_provenance
