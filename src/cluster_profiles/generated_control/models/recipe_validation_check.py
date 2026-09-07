from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_validation_check_assertions_item import check_recipe_validation_check_assertions_item
from ..models.recipe_validation_check_assertions_item import RecipeValidationCheckAssertionsItem
from ..models.recipe_validation_check_kind import check_recipe_validation_check_kind
from ..models.recipe_validation_check_kind import RecipeValidationCheckKind
from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.recipe_http_serving_request import RecipeHttpServingRequest
  from ..models.recipe_job_serving_request import RecipeJobServingRequest





T = TypeVar("T", bound="RecipeValidationCheck")



@_attrs_define
class RecipeValidationCheck:
    """
        Attributes:
            assertions (list[RecipeValidationCheckAssertionsItem]):
            kind (RecipeValidationCheckKind):
            name (str):
            request (Union['RecipeHttpServingRequest', 'RecipeJobServingRequest']):
     """

    assertions: list[RecipeValidationCheckAssertionsItem]
    kind: RecipeValidationCheckKind
    name: str
    request: Union['RecipeHttpServingRequest', 'RecipeJobServingRequest']





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_http_serving_request import RecipeHttpServingRequest
        from ..models.recipe_job_serving_request import RecipeJobServingRequest
        assertions = []
        for assertions_item_data in self.assertions:
            assertions_item: str = assertions_item_data
            assertions.append(assertions_item)



        kind: str = self.kind

        name = self.name

        request: dict[str, Any]
        if isinstance(self.request, RecipeHttpServingRequest):
            request = self.request.to_dict()
        else:
            request = self.request.to_dict()



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assertions": assertions,
            "kind": kind,
            "name": name,
            "request": request,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_http_serving_request import RecipeHttpServingRequest
        from ..models.recipe_job_serving_request import RecipeJobServingRequest
        d = dict(src_dict)
        assertions = []
        _assertions = d.pop("assertions")
        for assertions_item_data in (_assertions):
            assertions_item = check_recipe_validation_check_assertions_item(assertions_item_data)



            assertions.append(assertions_item)


        kind = check_recipe_validation_check_kind(d.pop("kind"))




        name = d.pop("name")

        def _parse_request(data: object) -> Union['RecipeHttpServingRequest', 'RecipeJobServingRequest']:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                request_type_0 = RecipeHttpServingRequest.from_dict(data)



                return request_type_0
            except: # noqa: E722
                pass
            if not isinstance(data, dict):
                raise TypeError()
            request_type_1 = RecipeJobServingRequest.from_dict(data)



            return request_type_1

        request = _parse_request(d.pop("request"))


        recipe_validation_check = cls(
            assertions=assertions,
            kind=kind,
            name=name,
            request=request,
        )

        return recipe_validation_check
