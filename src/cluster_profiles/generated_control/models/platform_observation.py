from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.platform_observation_worker_issue_type_0 import check_platform_observation_worker_issue_type_0
from ..models.platform_observation_worker_issue_type_0 import PlatformObservationWorkerIssueType0
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.api_runtime_observation import ApiRuntimeObservation
  from ..models.worker_runtime_observation import WorkerRuntimeObservation





T = TypeVar("T", bound="PlatformObservation")



@_attrs_define
class PlatformObservation:
    """
        Attributes:
            api (ApiRuntimeObservation):
            observed_at (datetime.datetime):
            worker_issue (None | PlatformObservationWorkerIssueType0):
            workers (list[WorkerRuntimeObservation] | None):
     """

    api: ApiRuntimeObservation
    observed_at: datetime.datetime
    worker_issue: None | PlatformObservationWorkerIssueType0
    workers: list[WorkerRuntimeObservation] | None





    def to_dict(self) -> dict[str, Any]:
        from ..models.api_runtime_observation import ApiRuntimeObservation # noqa: PLC0415
        from ..models.worker_runtime_observation import WorkerRuntimeObservation # noqa: PLC0415
        api = self.api.to_dict()

        observed_at = self.observed_at.isoformat()

        worker_issue: None | str
        if isinstance(self.worker_issue, str):
            worker_issue = self.worker_issue
        else:
            worker_issue = self.worker_issue

        workers: list[dict[str, Any]] | None
        if isinstance(self.workers, list):
            workers = []
            for workers_type_0_item_data in self.workers:
                workers_type_0_item = workers_type_0_item_data.to_dict()
                workers.append(workers_type_0_item)


        else:
            workers = self.workers


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "api": api,
            "observed_at": observed_at,
            "worker_issue": worker_issue,
            "workers": workers,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.api_runtime_observation import ApiRuntimeObservation # noqa: PLC0415
        from ..models.worker_runtime_observation import WorkerRuntimeObservation # noqa: PLC0415
        d = dict(src_dict)
        api = ApiRuntimeObservation.from_dict(d.pop("api"))




        observed_at = datetime.datetime.fromisoformat(d.pop("observed_at"))




        def _parse_worker_issue(data: object) -> None | PlatformObservationWorkerIssueType0:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                worker_issue_type_0 = check_platform_observation_worker_issue_type_0(data)



                return worker_issue_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlatformObservationWorkerIssueType0, data)

        worker_issue = _parse_worker_issue(d.pop("worker_issue"))


        def _parse_workers(data: object) -> list[WorkerRuntimeObservation] | None:
            if data is None:
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                workers_type_0 = []
                _workers_type_0 = data
                for workers_type_0_item_data in (_workers_type_0):
                    workers_type_0_item = WorkerRuntimeObservation.from_dict(workers_type_0_item_data)



                    workers_type_0.append(workers_type_0_item)

                return workers_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[WorkerRuntimeObservation] | None, data)

        workers = _parse_workers(d.pop("workers"))


        platform_observation = cls(
            api=api,
            observed_at=observed_at,
            worker_issue=worker_issue,
            workers=workers,
        )

        return platform_observation
