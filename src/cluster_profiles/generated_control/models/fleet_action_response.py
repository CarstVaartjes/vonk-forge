from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_action_response_action import check_fleet_action_response_action
from ..models.fleet_action_response_action import FleetActionResponseAction
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.deployment_provenance import DeploymentProvenance
  from ..models.enrollment_grant_response import EnrollmentGrantResponse





T = TypeVar("T", bound="FleetActionResponse")



@_attrs_define
class FleetActionResponse:
    """
        Attributes:
            action (FleetActionResponseAction):
            state (str):
            detail (None | str | Unset):
            display_name (None | str | Unset):
            grant (EnrollmentGrantResponse | None | Unset):
            node_id (None | str | Unset):
            operation_id (None | str | Unset):
            plan_digest (None | str | Unset):
            provenance (DeploymentProvenance | None | Unset):
            request_key (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
            targets (list[str] | Unset):
     """

    action: FleetActionResponseAction
    state: str
    detail: None | str | Unset = UNSET
    display_name: None | str | Unset = UNSET
    grant: EnrollmentGrantResponse | None | Unset = UNSET
    node_id: None | str | Unset = UNSET
    operation_id: None | str | Unset = UNSET
    plan_digest: None | str | Unset = UNSET
    provenance: DeploymentProvenance | None | Unset = UNSET
    request_key: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2
    targets: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.deployment_provenance import DeploymentProvenance # noqa: PLC0415
        from ..models.enrollment_grant_response import EnrollmentGrantResponse # noqa: PLC0415
        action: str = self.action

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

        node_id: None | str | Unset
        if isinstance(self.node_id, Unset):
            node_id = UNSET
        else:
            node_id = self.node_id

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

        provenance: dict[str, Any] | None | Unset
        if isinstance(self.provenance, Unset):
            provenance = UNSET
        elif isinstance(self.provenance, DeploymentProvenance):
            provenance = self.provenance.to_dict()
        else:
            provenance = self.provenance

        request_key: None | str | Unset
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key

        schema_version = self.schema_version

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
        if node_id is not UNSET:
            field_dict["node_id"] = node_id
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if provenance is not UNSET:
            field_dict["provenance"] = provenance
        if request_key is not UNSET:
            field_dict["request_key"] = request_key
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if targets is not UNSET:
            field_dict["targets"] = targets

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deployment_provenance import DeploymentProvenance # noqa: PLC0415
        from ..models.enrollment_grant_response import EnrollmentGrantResponse # noqa: PLC0415
        d = dict(src_dict)
        action = check_fleet_action_response_action(d.pop("action"))




        state = d.pop("state")

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


        def _parse_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        node_id = _parse_node_id(d.pop("node_id", UNSET))


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


        def _parse_provenance(data: object) -> DeploymentProvenance | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                provenance_type_0 = DeploymentProvenance.from_dict(data)



                return provenance_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DeploymentProvenance | None | Unset, data)

        provenance = _parse_provenance(d.pop("provenance", UNSET))


        def _parse_request_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        targets = cast(list[str], d.pop("targets", UNSET))


        fleet_action_response = cls(
            action=action,
            state=state,
            detail=detail,
            display_name=display_name,
            grant=grant,
            node_id=node_id,
            operation_id=operation_id,
            plan_digest=plan_digest,
            provenance=provenance,
            request_key=request_key,
            schema_version=schema_version,
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
