from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_workload_state import check_telemetry_workload_state
from ..models.telemetry_workload_state import TelemetryWorkloadState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="TelemetryWorkload")



@_attrs_define
class TelemetryWorkload:
    """ Sanitized request/job correlation to the actual serving placement.

        Attributes:
            engine_id (str):
            executor_node_ids (list[str]):
            run_id (str):
            state (TelemetryWorkloadState):
            created_at (Union[None, Unset, datetime.datetime]):
            elapsed_seconds (Union[None, Unset, float]):
            ended_at (Union[None, Unset, datetime.datetime]):
            eta_seconds (Union[None, Unset, float]):
            eta_source (Union[None, Unset, str]):
            failure (Union[None, Unset, str]):
            job_id (Union[None, Unset, str]):
            model (Union[None, Unset, str]):
            origin_node_id (Union[None, Unset, str]):
            progress_max (Union[None, Unset, float]):
            progress_value (Union[None, Unset, float]):
            recipe_revision (Union[None, Unset, str]):
            request_id (Union[None, Unset, str]):
            started_at (Union[None, Unset, datetime.datetime]):
            title (Union[None, Unset, str]):
     """

    engine_id: str
    executor_node_ids: list[str]
    run_id: str
    state: TelemetryWorkloadState
    created_at: Union[None, Unset, datetime.datetime] = UNSET
    elapsed_seconds: Union[None, Unset, float] = UNSET
    ended_at: Union[None, Unset, datetime.datetime] = UNSET
    eta_seconds: Union[None, Unset, float] = UNSET
    eta_source: Union[None, Unset, str] = UNSET
    failure: Union[None, Unset, str] = UNSET
    job_id: Union[None, Unset, str] = UNSET
    model: Union[None, Unset, str] = UNSET
    origin_node_id: Union[None, Unset, str] = UNSET
    progress_max: Union[None, Unset, float] = UNSET
    progress_value: Union[None, Unset, float] = UNSET
    recipe_revision: Union[None, Unset, str] = UNSET
    request_id: Union[None, Unset, str] = UNSET
    started_at: Union[None, Unset, datetime.datetime] = UNSET
    title: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        engine_id = self.engine_id

        executor_node_ids = self.executor_node_ids



        run_id = self.run_id

        state: str = self.state

        created_at: Union[None, Unset, str]
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        elif isinstance(self.created_at, datetime.datetime):
            created_at = self.created_at.isoformat()
        else:
            created_at = self.created_at

        elapsed_seconds: Union[None, Unset, float]
        if isinstance(self.elapsed_seconds, Unset):
            elapsed_seconds = UNSET
        else:
            elapsed_seconds = self.elapsed_seconds

        ended_at: Union[None, Unset, str]
        if isinstance(self.ended_at, Unset):
            ended_at = UNSET
        elif isinstance(self.ended_at, datetime.datetime):
            ended_at = self.ended_at.isoformat()
        else:
            ended_at = self.ended_at

        eta_seconds: Union[None, Unset, float]
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        eta_source: Union[None, Unset, str]
        if isinstance(self.eta_source, Unset):
            eta_source = UNSET
        else:
            eta_source = self.eta_source

        failure: Union[None, Unset, str]
        if isinstance(self.failure, Unset):
            failure = UNSET
        else:
            failure = self.failure

        job_id: Union[None, Unset, str]
        if isinstance(self.job_id, Unset):
            job_id = UNSET
        else:
            job_id = self.job_id

        model: Union[None, Unset, str]
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        origin_node_id: Union[None, Unset, str]
        if isinstance(self.origin_node_id, Unset):
            origin_node_id = UNSET
        else:
            origin_node_id = self.origin_node_id

        progress_max: Union[None, Unset, float]
        if isinstance(self.progress_max, Unset):
            progress_max = UNSET
        else:
            progress_max = self.progress_max

        progress_value: Union[None, Unset, float]
        if isinstance(self.progress_value, Unset):
            progress_value = UNSET
        else:
            progress_value = self.progress_value

        recipe_revision: Union[None, Unset, str]
        if isinstance(self.recipe_revision, Unset):
            recipe_revision = UNSET
        else:
            recipe_revision = self.recipe_revision

        request_id: Union[None, Unset, str]
        if isinstance(self.request_id, Unset):
            request_id = UNSET
        else:
            request_id = self.request_id

        started_at: Union[None, Unset, str]
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        elif isinstance(self.started_at, datetime.datetime):
            started_at = self.started_at.isoformat()
        else:
            started_at = self.started_at

        title: Union[None, Unset, str]
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "engine_id": engine_id,
            "executor_node_ids": executor_node_ids,
            "run_id": run_id,
            "state": state,
        })
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if elapsed_seconds is not UNSET:
            field_dict["elapsed_seconds"] = elapsed_seconds
        if ended_at is not UNSET:
            field_dict["ended_at"] = ended_at
        if eta_seconds is not UNSET:
            field_dict["eta_seconds"] = eta_seconds
        if eta_source is not UNSET:
            field_dict["eta_source"] = eta_source
        if failure is not UNSET:
            field_dict["failure"] = failure
        if job_id is not UNSET:
            field_dict["job_id"] = job_id
        if model is not UNSET:
            field_dict["model"] = model
        if origin_node_id is not UNSET:
            field_dict["origin_node_id"] = origin_node_id
        if progress_max is not UNSET:
            field_dict["progress_max"] = progress_max
        if progress_value is not UNSET:
            field_dict["progress_value"] = progress_value
        if recipe_revision is not UNSET:
            field_dict["recipe_revision"] = recipe_revision
        if request_id is not UNSET:
            field_dict["request_id"] = request_id
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if title is not UNSET:
            field_dict["title"] = title

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        engine_id = d.pop("engine_id")

        executor_node_ids = cast(list[str], d.pop("executor_node_ids"))


        run_id = d.pop("run_id")

        state = check_telemetry_workload_state(d.pop("state"))




        def _parse_created_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                created_at_type_0 = isoparse(data)



                return created_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))


        def _parse_elapsed_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        elapsed_seconds = _parse_elapsed_seconds(d.pop("elapsed_seconds", UNSET))


        def _parse_ended_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                ended_at_type_0 = isoparse(data)



                return ended_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        ended_at = _parse_ended_at(d.pop("ended_at", UNSET))


        def _parse_eta_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        def _parse_eta_source(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        eta_source = _parse_eta_source(d.pop("eta_source", UNSET))


        def _parse_failure(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_job_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        job_id = _parse_job_id(d.pop("job_id", UNSET))


        def _parse_model(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model = _parse_model(d.pop("model", UNSET))


        def _parse_origin_node_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        origin_node_id = _parse_origin_node_id(d.pop("origin_node_id", UNSET))


        def _parse_progress_max(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        progress_max = _parse_progress_max(d.pop("progress_max", UNSET))


        def _parse_progress_value(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        progress_value = _parse_progress_value(d.pop("progress_value", UNSET))


        def _parse_recipe_revision(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_revision = _parse_recipe_revision(d.pop("recipe_revision", UNSET))


        def _parse_request_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        request_id = _parse_request_id(d.pop("request_id", UNSET))


        def _parse_started_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                started_at_type_0 = isoparse(data)



                return started_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))


        def _parse_title(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        title = _parse_title(d.pop("title", UNSET))


        telemetry_workload = cls(
            engine_id=engine_id,
            executor_node_ids=executor_node_ids,
            run_id=run_id,
            state=state,
            created_at=created_at,
            elapsed_seconds=elapsed_seconds,
            ended_at=ended_at,
            eta_seconds=eta_seconds,
            eta_source=eta_source,
            failure=failure,
            job_id=job_id,
            model=model,
            origin_node_id=origin_node_id,
            progress_max=progress_max,
            progress_value=progress_value,
            recipe_revision=recipe_revision,
            request_id=request_id,
            started_at=started_at,
            title=title,
        )

        return telemetry_workload
