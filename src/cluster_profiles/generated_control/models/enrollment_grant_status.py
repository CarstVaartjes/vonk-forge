from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.enrollment_grant_status_purpose import check_enrollment_grant_status_purpose
from ..models.enrollment_grant_status_purpose import EnrollmentGrantStatusPurpose
from ..models.enrollment_grant_status_state import check_enrollment_grant_status_state
from ..models.enrollment_grant_status_state import EnrollmentGrantStatusState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime






T = TypeVar("T", bound="EnrollmentGrantStatus")



@_attrs_define
class EnrollmentGrantStatus:
    """
        Attributes:
            consumed_at (datetime.datetime | None):
            display_name (None | str):
            expires_at (datetime.datetime):
            id (str):
            node_id (None | str):
            purpose (EnrollmentGrantStatusPurpose):
            revoked_at (datetime.datetime | None):
            state (EnrollmentGrantStatusState):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    consumed_at: datetime.datetime | None
    display_name: None | str
    expires_at: datetime.datetime
    id: str
    node_id: None | str
    purpose: EnrollmentGrantStatusPurpose
    revoked_at: datetime.datetime | None
    state: EnrollmentGrantStatusState
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        consumed_at: None | str
        if isinstance(self.consumed_at, datetime.datetime):
            consumed_at = self.consumed_at.isoformat()
        else:
            consumed_at = self.consumed_at

        display_name: None | str
        display_name = self.display_name

        expires_at = self.expires_at.isoformat()

        id = self.id

        node_id: None | str
        node_id = self.node_id

        purpose: str = self.purpose

        revoked_at: None | str
        if isinstance(self.revoked_at, datetime.datetime):
            revoked_at = self.revoked_at.isoformat()
        else:
            revoked_at = self.revoked_at

        state: str = self.state

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "consumed_at": consumed_at,
            "display_name": display_name,
            "expires_at": expires_at,
            "id": id,
            "node_id": node_id,
            "purpose": purpose,
            "revoked_at": revoked_at,
            "state": state,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_consumed_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                consumed_at_type_0 = datetime.datetime.fromisoformat(data)



                return consumed_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        consumed_at = _parse_consumed_at(d.pop("consumed_at"))


        def _parse_display_name(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        display_name = _parse_display_name(d.pop("display_name"))


        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))




        id = d.pop("id")

        def _parse_node_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        node_id = _parse_node_id(d.pop("node_id"))


        purpose = check_enrollment_grant_status_purpose(d.pop("purpose"))




        def _parse_revoked_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                revoked_at_type_0 = datetime.datetime.fromisoformat(data)



                return revoked_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        revoked_at = _parse_revoked_at(d.pop("revoked_at"))


        state = check_enrollment_grant_status_state(d.pop("state"))




        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        enrollment_grant_status = cls(
            consumed_at=consumed_at,
            display_name=display_name,
            expires_at=expires_at,
            id=id,
            node_id=node_id,
            purpose=purpose,
            revoked_at=revoked_at,
            state=state,
            schema_version=schema_version,
        )

        return enrollment_grant_status
