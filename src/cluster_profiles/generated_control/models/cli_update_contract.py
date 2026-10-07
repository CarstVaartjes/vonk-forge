from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.cli_update_contract_worker_compatibility import check_cli_update_contract_worker_compatibility
from ..models.cli_update_contract_worker_compatibility import CliUpdateContractWorkerCompatibility
from ..models.cli_update_contract_worker_issue_type_0 import check_cli_update_contract_worker_issue_type_0
from ..models.cli_update_contract_worker_issue_type_0 import CliUpdateContractWorkerIssueType0
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.api_runtime_observation import ApiRuntimeObservation





T = TypeVar("T", bound="CliUpdateContract")



@_attrs_define
class CliUpdateContract:
    """
        Attributes:
            api (ApiRuntimeObservation):
            compatibility_schema_sha256 (str):
            expected_worker_contract_sha256 (None | str):
            observed_at (datetime.datetime):
            worker_compatibility (CliUpdateContractWorkerCompatibility):
            worker_contract_sha256 (None | str):
            worker_count (int):
            worker_issue (CliUpdateContractWorkerIssueType0 | None):
            worker_membership_sha256 (str):
            worker_source_sha (None | str):
     """

    api: ApiRuntimeObservation
    compatibility_schema_sha256: str
    expected_worker_contract_sha256: None | str
    observed_at: datetime.datetime
    worker_compatibility: CliUpdateContractWorkerCompatibility
    worker_contract_sha256: None | str
    worker_count: int
    worker_issue: CliUpdateContractWorkerIssueType0 | None
    worker_membership_sha256: str
    worker_source_sha: None | str





    def to_dict(self) -> dict[str, Any]:
        from ..models.api_runtime_observation import ApiRuntimeObservation # noqa: PLC0415
        api = self.api.to_dict()

        compatibility_schema_sha256 = self.compatibility_schema_sha256

        expected_worker_contract_sha256: None | str
        expected_worker_contract_sha256 = self.expected_worker_contract_sha256

        observed_at = self.observed_at.isoformat()

        worker_compatibility: str = self.worker_compatibility

        worker_contract_sha256: None | str
        worker_contract_sha256 = self.worker_contract_sha256

        worker_count = self.worker_count

        worker_issue: None | str
        if isinstance(self.worker_issue, str):
            worker_issue = self.worker_issue
        else:
            worker_issue = self.worker_issue

        worker_membership_sha256 = self.worker_membership_sha256

        worker_source_sha: None | str
        worker_source_sha = self.worker_source_sha


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "api": api,
            "compatibility_schema_sha256": compatibility_schema_sha256,
            "expected_worker_contract_sha256": expected_worker_contract_sha256,
            "observed_at": observed_at,
            "worker_compatibility": worker_compatibility,
            "worker_contract_sha256": worker_contract_sha256,
            "worker_count": worker_count,
            "worker_issue": worker_issue,
            "worker_membership_sha256": worker_membership_sha256,
            "worker_source_sha": worker_source_sha,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.api_runtime_observation import ApiRuntimeObservation # noqa: PLC0415
        d = dict(src_dict)
        api = ApiRuntimeObservation.from_dict(d.pop("api"))




        compatibility_schema_sha256 = d.pop("compatibility_schema_sha256")

        def _parse_expected_worker_contract_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        expected_worker_contract_sha256 = _parse_expected_worker_contract_sha256(d.pop("expected_worker_contract_sha256"))


        observed_at = datetime.datetime.fromisoformat(d.pop("observed_at"))




        worker_compatibility = check_cli_update_contract_worker_compatibility(d.pop("worker_compatibility"))




        def _parse_worker_contract_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        worker_contract_sha256 = _parse_worker_contract_sha256(d.pop("worker_contract_sha256"))


        worker_count = d.pop("worker_count")

        def _parse_worker_issue(data: object) -> CliUpdateContractWorkerIssueType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                worker_issue_type_0 = check_cli_update_contract_worker_issue_type_0(data)



                return worker_issue_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CliUpdateContractWorkerIssueType0 | None, data)

        worker_issue = _parse_worker_issue(d.pop("worker_issue"))


        worker_membership_sha256 = d.pop("worker_membership_sha256")

        def _parse_worker_source_sha(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        worker_source_sha = _parse_worker_source_sha(d.pop("worker_source_sha"))


        cli_update_contract = cls(
            api=api,
            compatibility_schema_sha256=compatibility_schema_sha256,
            expected_worker_contract_sha256=expected_worker_contract_sha256,
            observed_at=observed_at,
            worker_compatibility=worker_compatibility,
            worker_contract_sha256=worker_contract_sha256,
            worker_count=worker_count,
            worker_issue=worker_issue,
            worker_membership_sha256=worker_membership_sha256,
            worker_source_sha=worker_source_sha,
        )

        return cli_update_contract
