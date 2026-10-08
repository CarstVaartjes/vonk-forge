from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.enrollment_grant_state import check_enrollment_grant_state
from ..models.enrollment_grant_state import EnrollmentGrantState
from ..models.fleet_action_response_action import check_fleet_action_response_action
from ..models.fleet_action_response_action import FleetActionResponseAction
from ..models.lifecycle_state import check_lifecycle_state
from ..models.lifecycle_state import LifecycleState
from ..models.model_cache_operator_status import check_model_cache_operator_status
from ..models.model_cache_operator_status import ModelCacheOperatorStatus
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.enrollment_grant_response import EnrollmentGrantResponse
  from ..models.enrollment_grant_status import EnrollmentGrantStatus
  from ..models.enrollment_observation_outcome import EnrollmentObservationOutcome
  from ..models.enrollment_revocation_status import EnrollmentRevocationStatus





T = TypeVar("T", bound="FleetActionResponse")



@_attrs_define
class FleetActionResponse:
    """
        Attributes:
            action (FleetActionResponseAction):
            state (EnrollmentGrantState | LifecycleState | ModelCacheOperatorStatus):
            detail (None | str | Unset):
            display_name (None | str | Unset):
            grant (EnrollmentGrantResponse | None | Unset):
            grant_status (EnrollmentGrantStatus | None | Unset):
            node_id (None | str | Unset):
            observation (EnrollmentObservationOutcome | None | Unset):
            operation_id (None | str | Unset):
            plan_digest (None | str | Unset):
            request_key (None | str | Unset):
            revocation (EnrollmentRevocationStatus | None | Unset):
            targets (list[str] | Unset):
     """

    action: FleetActionResponseAction
    state: EnrollmentGrantState | LifecycleState | ModelCacheOperatorStatus
    detail: None | str | Unset = UNSET
    display_name: None | str | Unset = UNSET
    grant: EnrollmentGrantResponse | None | Unset = UNSET
    grant_status: EnrollmentGrantStatus | None | Unset = UNSET
    node_id: None | str | Unset = UNSET
    observation: EnrollmentObservationOutcome | None | Unset = UNSET
    operation_id: None | str | Unset = UNSET
    plan_digest: None | str | Unset = UNSET
    request_key: None | str | Unset = UNSET
    revocation: EnrollmentRevocationStatus | None | Unset = UNSET
    targets: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.enrollment_grant_response import EnrollmentGrantResponse # noqa: PLC0415
        from ..models.enrollment_grant_status import EnrollmentGrantStatus # noqa: PLC0415
        from ..models.enrollment_observation_outcome import EnrollmentObservationOutcome # noqa: PLC0415
        from ..models.enrollment_revocation_status import EnrollmentRevocationStatus # noqa: PLC0415
        action: str = self.action

        state: str
        if isinstance(self.state, str):
            state = self.state
        elif isinstance(self.state, str):
            state = self.state
        else:
            state = self.state


        detail: None | str | Unset
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        display_name: None | str | Unset
        if isinstance(self.display_name, Unset):
            display_name = UNSET
        else:
            display_name = self.display_name

        grant: dict[str, Any] | None | Unset
        if isinstance(self.grant, Unset):
            grant = UNSET
        elif isinstance(self.grant, EnrollmentGrantResponse):
            grant = self.grant.to_dict()
        else:
            grant = self.grant

        grant_status: dict[str, Any] | None | Unset
        if isinstance(self.grant_status, Unset):
            grant_status = UNSET
        elif isinstance(self.grant_status, EnrollmentGrantStatus):
            grant_status = self.grant_status.to_dict()
        else:
            grant_status = self.grant_status

        node_id: None | str | Unset
        if isinstance(self.node_id, Unset):
            node_id = UNSET
        else:
            node_id = self.node_id

        observation: dict[str, Any] | None | Unset
        if isinstance(self.observation, Unset):
            observation = UNSET
        elif isinstance(self.observation, EnrollmentObservationOutcome):
            observation = self.observation.to_dict()
        else:
            observation = self.observation

        operation_id: None | str | Unset
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        plan_digest: None | str | Unset
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        request_key: None | str | Unset
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key

        revocation: dict[str, Any] | None | Unset
        if isinstance(self.revocation, Unset):
            revocation = UNSET
        elif isinstance(self.revocation, EnrollmentRevocationStatus):
            revocation = self.revocation.to_dict()
        else:
            revocation = self.revocation

        targets: list[str] | Unset = UNSET
        if not isinstance(self.targets, Unset):
            targets = self.targets




        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "action": action,
            "state": state,
        })
        if detail is not UNSET:
            field_dict["detail"] = detail
        if display_name is not UNSET:
            field_dict["display_name"] = display_name
        if grant is not UNSET:
            field_dict["grant"] = grant
        if grant_status is not UNSET:
            field_dict["grant_status"] = grant_status
        if node_id is not UNSET:
            field_dict["node_id"] = node_id
        if observation is not UNSET:
            field_dict["observation"] = observation
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if request_key is not UNSET:
            field_dict["request_key"] = request_key
        if revocation is not UNSET:
            field_dict["revocation"] = revocation
        if targets is not UNSET:
            field_dict["targets"] = targets

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.enrollment_grant_response import EnrollmentGrantResponse # noqa: PLC0415
        from ..models.enrollment_grant_status import EnrollmentGrantStatus # noqa: PLC0415
        from ..models.enrollment_observation_outcome import EnrollmentObservationOutcome # noqa: PLC0415
        from ..models.enrollment_revocation_status import EnrollmentRevocationStatus # noqa: PLC0415
        d = dict(src_dict)
        action = check_fleet_action_response_action(d.pop("action"))




        def _parse_state(data: object) -> EnrollmentGrantState | LifecycleState | ModelCacheOperatorStatus:
            try:
                if not isinstance(data, str):
                    raise TypeError()
                state_type_0 = check_enrollment_grant_state(data)



                return state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, str):
                    raise TypeError()
                state_type_1 = check_lifecycle_state(data)



                return state_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, str):
                raise TypeError()
            state_type_2 = check_model_cache_operator_status(data)



            return state_type_2

        state = _parse_state(d.pop("state"))


        def _parse_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        detail = _parse_detail(d.pop("detail", UNSET))


        def _parse_display_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        display_name = _parse_display_name(d.pop("display_name", UNSET))


        def _parse_grant(data: object) -> EnrollmentGrantResponse | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                grant_type_0 = EnrollmentGrantResponse.from_dict(data)



                return grant_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EnrollmentGrantResponse | None | Unset, data)

        grant = _parse_grant(d.pop("grant", UNSET))


        def _parse_grant_status(data: object) -> EnrollmentGrantStatus | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                grant_status_type_0 = EnrollmentGrantStatus.from_dict(data)



                return grant_status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EnrollmentGrantStatus | None | Unset, data)

        grant_status = _parse_grant_status(d.pop("grant_status", UNSET))


        def _parse_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        node_id = _parse_node_id(d.pop("node_id", UNSET))


        def _parse_observation(data: object) -> EnrollmentObservationOutcome | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                observation_type_0 = EnrollmentObservationOutcome.from_dict(data)



                return observation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EnrollmentObservationOutcome | None | Unset, data)

        observation = _parse_observation(d.pop("observation", UNSET))


        def _parse_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        def _parse_plan_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_request_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        def _parse_revocation(data: object) -> EnrollmentRevocationStatus | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                revocation_type_0 = EnrollmentRevocationStatus.from_dict(data)



                return revocation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EnrollmentRevocationStatus | None | Unset, data)

        revocation = _parse_revocation(d.pop("revocation", UNSET))


        targets = cast(list[str], d.pop("targets", UNSET))


        fleet_action_response = cls(
            action=action,
            state=state,
            detail=detail,
            display_name=display_name,
            grant=grant,
            grant_status=grant_status,
            node_id=node_id,
            observation=observation,
            operation_id=operation_id,
            plan_digest=plan_digest,
            request_key=request_key,
            revocation=revocation,
            targets=targets,
        )


        fleet_action_response.additional_properties = d
        return fleet_action_response

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
