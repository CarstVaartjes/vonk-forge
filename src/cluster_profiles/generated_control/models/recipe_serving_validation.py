from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_serving_validation_interface import check_recipe_serving_validation_interface
from ..models.recipe_serving_validation_interface import RecipeServingValidationInterface
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_validation_check import RecipeValidationCheck





T = TypeVar("T", bound="RecipeServingValidation")



@_attrs_define
class RecipeServingValidation:
    """
        Attributes:
            checks (list['RecipeValidationCheck']):
            interface (RecipeServingValidationInterface):
     """

    checks: list['RecipeValidationCheck']
    interface: RecipeServingValidationInterface





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_validation_check import RecipeValidationCheck
        checks = []
        for checks_item_data in self.checks:
            checks_item = checks_item_data.to_dict()
            checks.append(checks_item)



        interface: str = self.interface


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "checks": checks,
            "interface": interface,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_validation_check import RecipeValidationCheck
        d = dict(src_dict)
        checks = []
        _checks = d.pop("checks")
        for checks_item_data in (_checks):
            checks_item = RecipeValidationCheck.from_dict(checks_item_data)



            checks.append(checks_item)


        interface = check_recipe_serving_validation_interface(d.pop("interface"))




        recipe_serving_validation = cls(
            checks=checks,
            interface=interface,
        )

        return recipe_serving_validation
