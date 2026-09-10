from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_log_entry_level import check_fleet_log_entry_level
from ..models.fleet_log_entry_level import FleetLogEntryLevel
from ..models.fleet_log_entry_source import check_fleet_log_entry_source
from ..models.fleet_log_entry_source import FleetLogEntrySource
from dateutil.parser import isoparse
from typing import cast
import datetime






T = TypeVar("T", bound="FleetLogEntry")



@_attrs_define
class FleetLogEntry:
    """
        Attributes:
            evidence_id (str):
            level (FleetLogEntryLevel):
            message (str):
            observed_at (datetime.datetime):
            source (FleetLogEntrySource):
     """

    evidence_id: str
    level: FleetLogEntryLevel
    message: str
    observed_at: datetime.datetime
    source: FleetLogEntrySource





    def to_dict(self) -> dict[str, Any]:
        evidence_id = self.evidence_id

        level: str = self.level

        message = self.message

        observed_at = self.observed_at.isoformat()

        source: str = self.source


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "evidence_id": evidence_id,
            "level": level,
            "message": message,
            "observed_at": observed_at,
            "source": source,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        evidence_id = d.pop("evidence_id")

        level = check_fleet_log_entry_level(d.pop("level"))




        message = d.pop("message")

        observed_at = isoparse(d.pop("observed_at"))




        source = check_fleet_log_entry_source(d.pop("source"))




        fleet_log_entry = cls(
            evidence_id=evidence_id,
            level=level,
            message=message,
            observed_at=observed_at,
            source=source,
        )

        return fleet_log_entry
