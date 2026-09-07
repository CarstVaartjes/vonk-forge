from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_http_serving_request_method import check_recipe_http_serving_request_method
from ..models.recipe_http_serving_request_method import RecipeHttpServingRequestMethod
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_http_serving_request_body_type_0 import RecipeHttpServingRequestBodyType0





T = TypeVar("T", bound="RecipeHttpServingRequest")



@_attrs_define
class RecipeHttpServingRequest:
    """
        Attributes:
            method (RecipeHttpServingRequestMethod):
            path (str):
            transport (Literal['http']):
            body (Union['RecipeHttpServingRequestBodyType0', None, Unset]):
     """

    method: RecipeHttpServingRequestMethod
    path: str
    transport: Literal['http']
    body: Union['RecipeHttpServingRequestBodyType0', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_http_serving_request_body_type_0 import RecipeHttpServingRequestBodyType0
        method: str = self.method

        path = self.path

        transport = self.transport

        body: Union[None, Unset, dict[str, Any]]
        if isinstance(self.body, Unset):
            body = UNSET
        elif isinstance(self.body, RecipeHttpServingRequestBodyType0):
            body = self.body.to_dict()
        else:
            body = self.body


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "method": method,
            "path": path,
            "transport": transport,
        })
        if body is not UNSET:
            field_dict["body"] = body

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_http_serving_request_body_type_0 import RecipeHttpServingRequestBodyType0
        d = dict(src_dict)
        method = check_recipe_http_serving_request_method(d.pop("method"))




        path = d.pop("path")

        transport = cast(Literal['http'] , d.pop("transport"))
        if transport != 'http':
            raise ValueError(f"transport must match const 'http', got '{transport}'")

        def _parse_body(data: object) -> Union['RecipeHttpServingRequestBodyType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                body_type_0 = RecipeHttpServingRequestBodyType0.from_dict(data)



                return body_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeHttpServingRequestBodyType0', None, Unset], data)

        body = _parse_body(d.pop("body", UNSET))


        recipe_http_serving_request = cls(
            method=method,
            path=path,
            transport=transport,
            body=body,
        )

        return recipe_http_serving_request
