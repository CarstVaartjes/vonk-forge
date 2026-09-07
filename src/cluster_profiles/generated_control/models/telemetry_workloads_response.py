from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_workloads_response_freshness import check_telemetry_workloads_response_freshness
from ..models.telemetry_workloads_response_freshness import TelemetryWorkloadsResponseFreshness
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.telemetry_runtime import TelemetryRuntime
  from ..models.telemetry_workload import TelemetryWorkload





T = TypeVar("T", bound="TelemetryWorkloadsResponse")



@_attrs_define
class TelemetryWorkloadsResponse:
    """
        Attributes:
            freshness (TelemetryWorkloadsResponseFreshness):
            node_id (str):
            observed_at (datetime.datetime):
            received_at (datetime.datetime):
            runtimes (list['TelemetryRuntime']):
            workloads (list['TelemetryWorkload']):
            run_id (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            state (Union[None, Unset, str]):
     """

    freshness: TelemetryWorkloadsResponseFreshness
    node_id: str
    observed_at: datetime.datetime
    received_at: datetime.datetime
    runtimes: list['TelemetryRuntime']
    workloads: list['TelemetryWorkload']
    run_id: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    state: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_runtime import TelemetryRuntime
        from ..models.telemetry_workload import TelemetryWorkload
        freshness: str = self.freshness

        node_id = self.node_id

        observed_at = self.observed_at.isoformat()

        received_at = self.received_at.isoformat()

        runtimes = []
        for runtimes_item_data in self.runtimes:
            runtimes_item = runtimes_item_data.to_dict()
            runtimes.append(runtimes_item)



        workloads = []
        for workloads_item_data in self.workloads:
            workloads_item = workloads_item_data.to_dict()
            workloads.append(workloads_item)



        run_id: Union[None, Unset, str]
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        schema_version = self.schema_version

        state: Union[None, Unset, str]
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "freshness": freshness,
            "node_id": node_id,
            "observed_at": observed_at,
            "received_at": received_at,
            "runtimes": runtimes,
            "workloads": workloads,
        })
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_runtime import TelemetryRuntime
        from ..models.telemetry_workload import TelemetryWorkload
        d = dict(src_dict)
        freshness = check_telemetry_workloads_response_freshness(d.pop("freshness"))




        node_id = d.pop("node_id")

        observed_at = isoparse(d.pop("observed_at"))




        received_at = isoparse(d.pop("received_at"))




        runtimes = []
        _runtimes = d.pop("runtimes")
        for runtimes_item_data in (_runtimes):
            runtimes_item = TelemetryRuntime.from_dict(runtimes_item_data)



            runtimes.append(runtimes_item)


        workloads = []
        _workloads = d.pop("workloads")
        for workloads_item_data in (_workloads):
            workloads_item = TelemetryWorkload.from_dict(workloads_item_data)



            workloads.append(workloads_item)


        def _parse_run_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_state(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        state = _parse_state(d.pop("state", UNSET))


        telemetry_workloads_response = cls(
            freshness=freshness,
            node_id=node_id,
            observed_at=observed_at,
            received_at=received_at,
            runtimes=runtimes,
            workloads=workloads,
            run_id=run_id,
            schema_version=schema_version,
            state=state,
        )

        return telemetry_workloads_response
