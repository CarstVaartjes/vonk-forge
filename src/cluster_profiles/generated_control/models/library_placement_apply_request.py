from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_placement_apply_request_desired_state import check_library_placement_apply_request_desired_state
from ..models.library_placement_apply_request_desired_state import LibraryPlacementApplyRequestDesiredState
from ..models.library_placement_apply_request_invocation import check_library_placement_apply_request_invocation
from ..models.library_placement_apply_request_invocation import LibraryPlacementApplyRequestInvocation
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="LibraryPlacementApplyRequest")



@_attrs_define
class LibraryPlacementApplyRequest:
    """
        Attributes:
            node_ids (list[str]):
            plan_digest (str):
            recipe_id (str):
            request_key (str):
            alias (Union[None, Unset, str]):
            desired_state (Union[Unset, LibraryPlacementApplyRequestDesiredState]):  Default: 'installed'.
            invocation (Union[Unset, LibraryPlacementApplyRequestInvocation]):  Default: 'button'.
     """

    node_ids: list[str]
    plan_digest: str
    recipe_id: str
    request_key: str
    alias: Union[None, Unset, str] = UNSET
    desired_state: Union[Unset, LibraryPlacementApplyRequestDesiredState] = 'installed'
    invocation: Union[Unset, LibraryPlacementApplyRequestInvocation] = 'button'





    def to_dict(self) -> dict[str, Any]:
        node_ids = self.node_ids



        plan_digest = self.plan_digest

        recipe_id = self.recipe_id

        request_key = self.request_key

        alias: Union[None, Unset, str]
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias

        desired_state: Union[Unset, str] = UNSET
        if not isinstance(self.desired_state, Unset):
            desired_state = self.desired_state


        invocation: Union[Unset, str] = UNSET
        if not isinstance(self.invocation, Unset):
            invocation = self.invocation



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
            "plan_digest": plan_digest,
            "recipe_id": recipe_id,
            "request_key": request_key,
        })
        if alias is not UNSET:
            field_dict["alias"] = alias
        if desired_state is not UNSET:
            field_dict["desired_state"] = desired_state
        if invocation is not UNSET:
            field_dict["invocation"] = invocation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        plan_digest = d.pop("plan_digest")

        recipe_id = d.pop("recipe_id")

        request_key = d.pop("request_key")

        def _parse_alias(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        alias = _parse_alias(d.pop("alias", UNSET))


        _desired_state = d.pop("desired_state", UNSET)
        desired_state: Union[Unset, LibraryPlacementApplyRequestDesiredState]
        if isinstance(_desired_state,  Unset):
            desired_state = UNSET
        else:
            desired_state = check_library_placement_apply_request_desired_state(_desired_state)




        _invocation = d.pop("invocation", UNSET)
        invocation: Union[Unset, LibraryPlacementApplyRequestInvocation]
        if isinstance(_invocation,  Unset):
            invocation = UNSET
        else:
            invocation = check_library_placement_apply_request_invocation(_invocation)




        library_placement_apply_request = cls(
            node_ids=node_ids,
            plan_digest=plan_digest,
            recipe_id=recipe_id,
            request_key=request_key,
            alias=alias,
            desired_state=desired_state,
            invocation=invocation,
        )

        return library_placement_apply_request
