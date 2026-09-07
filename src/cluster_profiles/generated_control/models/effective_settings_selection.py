from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.effective_settings_selection_kind import check_effective_settings_selection_kind
from ..models.effective_settings_selection_kind import EffectiveSettingsSelectionKind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.effective_parallelism import EffectiveParallelism
  from ..models.effective_settings_selection_knobs import EffectiveSettingsSelectionKnobs
  from ..models.effective_settings_selection_change_effects import EffectiveSettingsSelectionChangeEffects





T = TypeVar("T", bound="EffectiveSettingsSelection")



@_attrs_define
class EffectiveSettingsSelection:
    """ Canonical effective settings bound into the Run/Switch plan digest.

        Attributes:
            change_effects (EffectiveSettingsSelectionChangeEffects):
            identity_sha256 (str):
            kind (EffectiveSettingsSelectionKind):
            parallelism (EffectiveParallelism): Derived from topology; never an editable settings field.
            concurrency (Union[None, Unset, int]):
            context_tokens (Union[None, Unset, int]):
            knobs (Union[Unset, EffectiveSettingsSelectionKnobs]):
            max_batch_tokens (Union[None, Unset, int]):
     """

    change_effects: 'EffectiveSettingsSelectionChangeEffects'
    identity_sha256: str
    kind: EffectiveSettingsSelectionKind
    parallelism: 'EffectiveParallelism'
    concurrency: Union[None, Unset, int] = UNSET
    context_tokens: Union[None, Unset, int] = UNSET
    knobs: Union[Unset, 'EffectiveSettingsSelectionKnobs'] = UNSET
    max_batch_tokens: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.effective_parallelism import EffectiveParallelism
        from ..models.effective_settings_selection_knobs import EffectiveSettingsSelectionKnobs
        from ..models.effective_settings_selection_change_effects import EffectiveSettingsSelectionChangeEffects
        change_effects = self.change_effects.to_dict()

        identity_sha256 = self.identity_sha256

        kind: str = self.kind

        parallelism = self.parallelism.to_dict()

        concurrency: Union[None, Unset, int]
        if isinstance(self.concurrency, Unset):
            concurrency = UNSET
        else:
            concurrency = self.concurrency

        context_tokens: Union[None, Unset, int]
        if isinstance(self.context_tokens, Unset):
            context_tokens = UNSET
        else:
            context_tokens = self.context_tokens

        knobs: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.knobs, Unset):
            knobs = self.knobs.to_dict()

        max_batch_tokens: Union[None, Unset, int]
        if isinstance(self.max_batch_tokens, Unset):
            max_batch_tokens = UNSET
        else:
            max_batch_tokens = self.max_batch_tokens


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "change_effects": change_effects,
            "identity_sha256": identity_sha256,
            "kind": kind,
            "parallelism": parallelism,
        })
        if concurrency is not UNSET:
            field_dict["concurrency"] = concurrency
        if context_tokens is not UNSET:
            field_dict["context_tokens"] = context_tokens
        if knobs is not UNSET:
            field_dict["knobs"] = knobs
        if max_batch_tokens is not UNSET:
            field_dict["max_batch_tokens"] = max_batch_tokens

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.effective_parallelism import EffectiveParallelism
        from ..models.effective_settings_selection_knobs import EffectiveSettingsSelectionKnobs
        from ..models.effective_settings_selection_change_effects import EffectiveSettingsSelectionChangeEffects
        d = dict(src_dict)
        change_effects = EffectiveSettingsSelectionChangeEffects.from_dict(d.pop("change_effects"))




        identity_sha256 = d.pop("identity_sha256")

        kind = check_effective_settings_selection_kind(d.pop("kind"))




        parallelism = EffectiveParallelism.from_dict(d.pop("parallelism"))




        def _parse_concurrency(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        concurrency = _parse_concurrency(d.pop("concurrency", UNSET))


        def _parse_context_tokens(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        context_tokens = _parse_context_tokens(d.pop("context_tokens", UNSET))


        _knobs = d.pop("knobs", UNSET)
        knobs: Union[Unset, EffectiveSettingsSelectionKnobs]
        if isinstance(_knobs,  Unset):
            knobs = UNSET
        else:
            knobs = EffectiveSettingsSelectionKnobs.from_dict(_knobs)




        def _parse_max_batch_tokens(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        max_batch_tokens = _parse_max_batch_tokens(d.pop("max_batch_tokens", UNSET))


        effective_settings_selection = cls(
            change_effects=change_effects,
            identity_sha256=identity_sha256,
            kind=kind,
            parallelism=parallelism,
            concurrency=concurrency,
            context_tokens=context_tokens,
            knobs=knobs,
            max_batch_tokens=max_batch_tokens,
        )

        return effective_settings_selection
