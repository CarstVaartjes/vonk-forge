from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_option_choice_env import RecipeOptionChoiceEnv
  from ..models.recipe_runtime_argument import RecipeRuntimeArgument





T = TypeVar("T", bound="RecipeOptionChoice")



@_attrs_define
class RecipeOptionChoice:
    """ One named value of an option, with the runtime changes it selects.

    ``args`` replace a base runtime argument of the same name in place, or are
    appended after the base arguments; ``env`` does the same for environment
    variables. Both go through the checks of ``runtime.arguments`` and
    ``runtime.environment``, and the platform applies its own security, mount
    and port rules to the merged result.

        Attributes:
            help_ (str):
            label (str):
            value (str):
            args (list[RecipeRuntimeArgument] | Unset):
            default (bool | Unset):  Default: False.
            env (RecipeOptionChoiceEnv | Unset):
     """

    help_: str
    label: str
    value: str
    args: list[RecipeRuntimeArgument] | Unset = UNSET
    default: bool | Unset = False
    env: RecipeOptionChoiceEnv | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_option_choice_env import RecipeOptionChoiceEnv # noqa: PLC0415
        from ..models.recipe_runtime_argument import RecipeRuntimeArgument # noqa: PLC0415
        help_ = self.help_

        label = self.label

        value = self.value

        args: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.args, Unset):
            args = []
            for args_item_data in self.args:
                args_item = args_item_data.to_dict()
                args.append(args_item)



        default = self.default

        env: dict[str, Any] | Unset = UNSET
        if not isinstance(self.env, Unset):
            env = self.env.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "help": help_,
            "label": label,
            "value": value,
        })
        if args is not UNSET:
            field_dict["args"] = args
        if default is not UNSET:
            field_dict["default"] = default
        if env is not UNSET:
            field_dict["env"] = env

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_option_choice_env import RecipeOptionChoiceEnv # noqa: PLC0415
        from ..models.recipe_runtime_argument import RecipeRuntimeArgument # noqa: PLC0415
        d = dict(src_dict)
        help_ = d.pop("help")

        label = d.pop("label")

        value = d.pop("value")

        _args = d.pop("args", UNSET)
        args: list[RecipeRuntimeArgument] | Unset = UNSET
        if _args is not UNSET:
            args = []
            for args_item_data in _args:
                args_item = RecipeRuntimeArgument.from_dict(args_item_data)



                args.append(args_item)


        default = d.pop("default", UNSET)

        _env = d.pop("env", UNSET)
        env: RecipeOptionChoiceEnv | Unset
        if isinstance(_env,  Unset):
            env = UNSET
        else:
            env = RecipeOptionChoiceEnv.from_dict(_env)




        recipe_option_choice = cls(
            help_=help_,
            label=label,
            value=value,
            args=args,
            default=default,
            env=env,
        )

        return recipe_option_choice
