from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union






T = TypeVar("T", bound="RequestValidationIssue")



@_attrs_define
class RequestValidationIssue:
    """ A structural input error without the submitted input or validator context.

        Attributes:
            loc (list[Union[int, str]]):
            msg (str):
            type_ (str):
     """

    loc: list[Union[int, str]]
    msg: str
    type_: str





    def to_dict(self) -> dict[str, Any]:
        loc = []
        for loc_item_data in self.loc:
            loc_item: Union[int, str]
            loc_item = loc_item_data
            loc.append(loc_item)



        msg = self.msg

        type_ = self.type_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "loc": loc,
            "msg": msg,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        loc = []
        _loc = d.pop("loc")
        for loc_item_data in (_loc):
            def _parse_loc_item(data: object) -> Union[int, str]:
                return cast(Union[int, str], data)

            loc_item = _parse_loc_item(loc_item_data)

            loc.append(loc_item)


        msg = d.pop("msg")

        type_ = d.pop("type")

        request_validation_issue = cls(
            loc=loc,
            msg=msg,
            type_=type_,
        )

        return request_validation_issue
