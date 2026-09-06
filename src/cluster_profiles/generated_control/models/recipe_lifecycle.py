from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_failure_policy import RecipeFailurePolicy





T = TypeVar("T", bound="RecipeLifecycle")



@_attrs_define
class RecipeLifecycle:
    """
        Attributes:
            post_stop (list[list[str]]):
            pre_start (list[list[str]]):
            stop_timeout_seconds (int):
            failure (Union['RecipeFailurePolicy', None, Unset]):
     """

    post_stop: list[list[str]]
    pre_start: list[list[str]]
    stop_timeout_seconds: int
    failure: Union['RecipeFailurePolicy', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_failure_policy import RecipeFailurePolicy
        post_stop = []
        for post_stop_item_data in self.post_stop:
            post_stop_item = post_stop_item_data


            post_stop.append(post_stop_item)



        pre_start = []
        for pre_start_item_data in self.pre_start:
            pre_start_item = pre_start_item_data


            pre_start.append(pre_start_item)



        stop_timeout_seconds = self.stop_timeout_seconds

        failure: Union[None, Unset, dict[str, Any]]
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, RecipeFailurePolicy):
            failure = self.failure.to_dict()
        else:
            failure = self.failure


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "post_stop": post_stop,
            "pre_start": pre_start,
            "stop_timeout_seconds": stop_timeout_seconds,
        })
        if failure is not UNSET:
            field_dict["failure"] = failure

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_failure_policy import RecipeFailurePolicy
        d = dict(src_dict)
        post_stop = []
        _post_stop = d.pop("post_stop")
        for post_stop_item_data in (_post_stop):
            post_stop_item = cast(list[str], post_stop_item_data)

            post_stop.append(post_stop_item)


        pre_start = []
        _pre_start = d.pop("pre_start")
        for pre_start_item_data in (_pre_start):
            pre_start_item = cast(list[str], pre_start_item_data)

            pre_start.append(pre_start_item)


        stop_timeout_seconds = d.pop("stop_timeout_seconds")

        def _parse_failure(data: object) -> Union['RecipeFailurePolicy', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = RecipeFailurePolicy.from_dict(data)



                return failure_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeFailurePolicy', None, Unset], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        recipe_lifecycle = cls(
            post_stop=post_stop,
            pre_start=pre_start,
            stop_timeout_seconds=stop_timeout_seconds,
            failure=failure,
        )

        return recipe_lifecycle
