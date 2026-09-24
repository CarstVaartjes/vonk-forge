from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_update_child_state import check_recipe_update_child_state
from ..models.recipe_update_child_state import RecipeUpdateChildState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.recipe_update_failure import RecipeUpdateFailure





T = TypeVar("T", bound="RecipeUpdateChild")



@_attrs_define
class RecipeUpdateChild:
    """ Frozen identity plus a rebuildable observation; child jobs own execution.

        Attributes:
            effective_execution_key (str):
            recipe_content_sha256 (str):
            recipe_name (str):
            recipe_revision_id (str):
            request_key (str):
            failure (Union['RecipeUpdateFailure', None, Unset]):
            observed_at (Union[None, Unset, datetime.datetime]):
            operation_id (Union[None, Unset, str]):
            retry_at (Union[None, Unset, datetime.datetime]):
            state (Union[Unset, RecipeUpdateChildState]):  Default: 'pending'.
     """

    effective_execution_key: str
    recipe_content_sha256: str
    recipe_name: str
    recipe_revision_id: str
    request_key: str
    failure: Union['RecipeUpdateFailure', None, Unset] = UNSET
    observed_at: Union[None, Unset, datetime.datetime] = UNSET
    operation_id: Union[None, Unset, str] = UNSET
    retry_at: Union[None, Unset, datetime.datetime] = UNSET
    state: Union[Unset, RecipeUpdateChildState] = 'pending'
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_update_failure import RecipeUpdateFailure
        effective_execution_key = self.effective_execution_key

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_name = self.recipe_name

        recipe_revision_id = self.recipe_revision_id

        request_key = self.request_key

        failure: Union[None, Unset, dict[str, Any]]
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, RecipeUpdateFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        observed_at: Union[None, Unset, str]
        if isinstance(self.observed_at, Unset):
            observed_at = UNSET
        elif isinstance(self.observed_at, datetime.datetime):
            observed_at = self.observed_at.isoformat()
        else:
            observed_at = self.observed_at

        operation_id: Union[None, Unset, str]
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        retry_at: Union[None, Unset, str]
        if isinstance(self.retry_at, Unset):
            retry_at = UNSET
        elif isinstance(self.retry_at, datetime.datetime):
            retry_at = self.retry_at.isoformat()
        else:
            retry_at = self.retry_at

        state: Union[Unset, str] = UNSET
        if not isinstance(self.state, Unset):
            state = self.state



        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "effective_execution_key": effective_execution_key,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_name": recipe_name,
            "recipe_revision_id": recipe_revision_id,
            "request_key": request_key,
        })
        if failure is not UNSET:
            field_dict["failure"] = failure
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if retry_at is not UNSET:
            field_dict["retry_at"] = retry_at
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_update_failure import RecipeUpdateFailure
        d = dict(src_dict)
        effective_execution_key = d.pop("effective_execution_key")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_name = d.pop("recipe_name")

        recipe_revision_id = d.pop("recipe_revision_id")

        request_key = d.pop("request_key")

        def _parse_failure(data: object) -> Union['RecipeUpdateFailure', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = RecipeUpdateFailure.from_dict(data)



                return failure_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeUpdateFailure', None, Unset], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_observed_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                observed_at_type_0 = isoparse(data)



                return observed_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))


        def _parse_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        def _parse_retry_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                retry_at_type_0 = isoparse(data)



                return retry_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        retry_at = _parse_retry_at(d.pop("retry_at", UNSET))


        _state = d.pop("state", UNSET)
        state: Union[Unset, RecipeUpdateChildState]
        if isinstance(_state,  Unset):
            state = UNSET
        else:
            state = check_recipe_update_child_state(_state)




        recipe_update_child = cls(
            effective_execution_key=effective_execution_key,
            recipe_content_sha256=recipe_content_sha256,
            recipe_name=recipe_name,
            recipe_revision_id=recipe_revision_id,
            request_key=request_key,
            failure=failure,
            observed_at=observed_at,
            operation_id=operation_id,
            retry_at=retry_at,
            state=state,
        )


        recipe_update_child.additional_properties = d
        return recipe_update_child

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
