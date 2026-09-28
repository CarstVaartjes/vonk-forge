from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_workload_state import check_telemetry_workload_state
from ..models.telemetry_workload_state import TelemetryWorkloadState
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="TelemetryWorkload")



@_attrs_define
class TelemetryWorkload:
    """
        Attributes:
            engine_id (str):
            executor_node_ids (list[str]):
            run_id (str):
            state (TelemetryWorkloadState):
            created_at (datetime.datetime | None | Unset):
            elapsed_seconds (float | None | Unset):
            ended_at (datetime.datetime | None | Unset):
            eta_seconds (float | None | Unset):
            eta_source (None | str | Unset):
            failure (None | str | Unset):
            job_id (None | str | Unset):
            model (None | str | Unset):
            origin_node_id (None | str | Unset):
            progress_max (float | None | Unset):
            progress_value (float | None | Unset):
            recipe_revision (None | str | Unset):
            request_id (None | str | Unset):
            started_at (datetime.datetime | None | Unset):
            title (None | str | Unset):
     """

    engine_id: str
    executor_node_ids: list[str]
    run_id: str
    state: TelemetryWorkloadState
    created_at: datetime.datetime | None | Unset = UNSET
    elapsed_seconds: float | None | Unset = UNSET
    ended_at: datetime.datetime | None | Unset = UNSET
    eta_seconds: float | None | Unset = UNSET
    eta_source: None | str | Unset = UNSET
    failure: None | str | Unset = UNSET
    job_id: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    origin_node_id: None | str | Unset = UNSET
    progress_max: float | None | Unset = UNSET
    progress_value: float | None | Unset = UNSET
    recipe_revision: None | str | Unset = UNSET
    request_id: None | str | Unset = UNSET
    started_at: datetime.datetime | None | Unset = UNSET
    title: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        engine_id = self.engine_id

        executor_node_ids = self.executor_node_ids



        run_id = self.run_id

        state: str = self.state

        created_at: None | str | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        elif isinstance(self.created_at, datetime.datetime):
            created_at = self.created_at.isoformat()
        else:
            created_at = self.created_at

        elapsed_seconds: float | None | Unset
        if isinstance(self.elapsed_seconds, Unset):
            elapsed_seconds = UNSET
        else:
            elapsed_seconds = self.elapsed_seconds

        ended_at: None | str | Unset
        if isinstance(self.ended_at, Unset):
            ended_at = UNSET
        elif isinstance(self.ended_at, datetime.datetime):
            ended_at = self.ended_at.isoformat()
        else:
            ended_at = self.ended_at

        eta_seconds: float | None | Unset
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        eta_source: None | str | Unset
        if isinstance(self.eta_source, Unset):
            eta_source = UNSET
        else:
            eta_source = self.eta_source

        failure: None | str | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        else:
            failure = self.failure

        job_id: None | str | Unset
        if isinstance(self.job_id, Unset):
            job_id = UNSET
        else:
            job_id = self.job_id

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        origin_node_id: None | str | Unset
        if isinstance(self.origin_node_id, Unset):
            origin_node_id = UNSET
        else:
            origin_node_id = self.origin_node_id

        progress_max: float | None | Unset
        if isinstance(self.progress_max, Unset):
            progress_max = UNSET
        else:
            progress_max = self.progress_max

        progress_value: float | None | Unset
        if isinstance(self.progress_value, Unset):
            progress_value = UNSET
        else:
            progress_value = self.progress_value

        recipe_revision: None | str | Unset
        if isinstance(self.recipe_revision, Unset):
            recipe_revision = UNSET
        else:
            recipe_revision = self.recipe_revision

        request_id: None | str | Unset
        if isinstance(self.request_id, Unset):
            request_id = UNSET
        else:
            request_id = self.request_id

        started_at: None | str | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        elif isinstance(self.started_at, datetime.datetime):
            started_at = self.started_at.isoformat()
        else:
            started_at = self.started_at

        title: None | str | Unset
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




        def _parse_created_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                created_at_type_0 = datetime.datetime.fromisoformat(data)



                return created_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))


        def _parse_elapsed_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        elapsed_seconds = _parse_elapsed_seconds(d.pop("elapsed_seconds", UNSET))


        def _parse_ended_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                ended_at_type_0 = datetime.datetime.fromisoformat(data)



                return ended_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        ended_at = _parse_ended_at(d.pop("ended_at", UNSET))


        def _parse_eta_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        def _parse_eta_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        eta_source = _parse_eta_source(d.pop("eta_source", UNSET))


        def _parse_failure(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_job_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        job_id = _parse_job_id(d.pop("job_id", UNSET))


        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))


        def _parse_origin_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        origin_node_id = _parse_origin_node_id(d.pop("origin_node_id", UNSET))


        def _parse_progress_max(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        progress_max = _parse_progress_max(d.pop("progress_max", UNSET))


        def _parse_progress_value(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        progress_value = _parse_progress_value(d.pop("progress_value", UNSET))


        def _parse_recipe_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_revision = _parse_recipe_revision(d.pop("recipe_revision", UNSET))


        def _parse_request_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_id = _parse_request_id(d.pop("request_id", UNSET))


        def _parse_started_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                started_at_type_0 = datetime.datetime.fromisoformat(data)



                return started_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))


        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

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
