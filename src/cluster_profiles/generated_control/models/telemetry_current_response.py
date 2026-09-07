from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_current_response_freshness import check_telemetry_current_response_freshness
from ..models.telemetry_current_response_freshness import TelemetryCurrentResponseFreshness
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.telemetry_point import TelemetryPoint





T = TypeVar("T", bound="TelemetryCurrentResponse")



@_attrs_define
class TelemetryCurrentResponse:
    """ Versioned current telemetry response with an explicit rich payload.

        Attributes:
            freshness (TelemetryCurrentResponseFreshness):
            node_id (str):
            observed_at (datetime.datetime):
            received_at (datetime.datetime):
            sample (TelemetryPoint):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    freshness: TelemetryCurrentResponseFreshness
    node_id: str
    observed_at: datetime.datetime
    received_at: datetime.datetime
    sample: 'TelemetryPoint'
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_point import TelemetryPoint
        freshness: str = self.freshness

        node_id = self.node_id

        observed_at = self.observed_at.isoformat()

        received_at = self.received_at.isoformat()

        sample = self.sample.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "freshness": freshness,
            "node_id": node_id,
            "observed_at": observed_at,
            "received_at": received_at,
            "sample": sample,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_point import TelemetryPoint
        d = dict(src_dict)
        freshness = check_telemetry_current_response_freshness(d.pop("freshness"))




        node_id = d.pop("node_id")

        observed_at = isoparse(d.pop("observed_at"))




        received_at = isoparse(d.pop("received_at"))




        sample = TelemetryPoint.from_dict(d.pop("sample"))




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        telemetry_current_response = cls(
            freshness=freshness,
            node_id=node_id,
            observed_at=observed_at,
            received_at=received_at,
            sample=sample,
            schema_version=schema_version,
        )

        return telemetry_current_response
