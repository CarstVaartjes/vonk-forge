from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_embedding_settings_knobs import RecipeEmbeddingSettingsKnobs
  from ..models.recipe_integer_setting import RecipeIntegerSetting





T = TypeVar("T", bound="RecipeEmbeddingSettings")



@_attrs_define
class RecipeEmbeddingSettings:
    """
        Attributes:
            kind (Literal['embedding']):
            concurrency (Union['RecipeIntegerSetting', None, Unset]):
            knobs (Union[Unset, RecipeEmbeddingSettingsKnobs]):
            max_batch_tokens (Union['RecipeIntegerSetting', None, Unset]):
     """

    kind: Literal['embedding']
    concurrency: Union['RecipeIntegerSetting', None, Unset] = UNSET
    knobs: Union[Unset, 'RecipeEmbeddingSettingsKnobs'] = UNSET
    max_batch_tokens: Union['RecipeIntegerSetting', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_embedding_settings_knobs import RecipeEmbeddingSettingsKnobs
        from ..models.recipe_integer_setting import RecipeIntegerSetting
        kind = self.kind

        concurrency: Union[None, Unset, dict[str, Any]]
        if isinstance(self.concurrency, Unset):
            concurrency = UNSET
        elif isinstance(self.concurrency, RecipeIntegerSetting):
            concurrency = self.concurrency.to_dict()
        else:
            concurrency = self.concurrency

        knobs: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.knobs, Unset):
            knobs = self.knobs.to_dict()

        max_batch_tokens: Union[None, Unset, dict[str, Any]]
        if isinstance(self.max_batch_tokens, Unset):
            max_batch_tokens = UNSET
        elif isinstance(self.max_batch_tokens, RecipeIntegerSetting):
            max_batch_tokens = self.max_batch_tokens.to_dict()
        else:
            max_batch_tokens = self.max_batch_tokens


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
        })
        if concurrency is not UNSET:
            field_dict["concurrency"] = concurrency
        if knobs is not UNSET:
            field_dict["knobs"] = knobs
        if max_batch_tokens is not UNSET:
            field_dict["max_batch_tokens"] = max_batch_tokens

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_embedding_settings_knobs import RecipeEmbeddingSettingsKnobs
        from ..models.recipe_integer_setting import RecipeIntegerSetting
        d = dict(src_dict)
        kind = cast(Literal['embedding'] , d.pop("kind"))
        if kind != 'embedding':
            raise ValueError(f"kind must match const 'embedding', got '{kind}'")

        def _parse_concurrency(data: object) -> Union['RecipeIntegerSetting', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                concurrency_type_0 = RecipeIntegerSetting.from_dict(data)



                return concurrency_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeIntegerSetting', None, Unset], data)

        concurrency = _parse_concurrency(d.pop("concurrency", UNSET))


        _knobs = d.pop("knobs", UNSET)
        knobs: Union[Unset, RecipeEmbeddingSettingsKnobs]
        if isinstance(_knobs,  Unset):
            knobs = UNSET
        else:
            knobs = RecipeEmbeddingSettingsKnobs.from_dict(_knobs)




        def _parse_max_batch_tokens(data: object) -> Union['RecipeIntegerSetting', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                max_batch_tokens_type_0 = RecipeIntegerSetting.from_dict(data)



                return max_batch_tokens_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeIntegerSetting', None, Unset], data)

        max_batch_tokens = _parse_max_batch_tokens(d.pop("max_batch_tokens", UNSET))


        recipe_embedding_settings = cls(
            kind=kind,
            concurrency=concurrency,
            knobs=knobs,
            max_batch_tokens=max_batch_tokens,
        )

        return recipe_embedding_settings
