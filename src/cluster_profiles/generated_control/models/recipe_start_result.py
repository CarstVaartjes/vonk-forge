from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.failure_diagnostics import FailureDiagnostics





T = TypeVar("T", bound="RecipeStartResult")



@_attrs_define
class RecipeStartResult:
    """ The serving rank reports its endpoint; every other rank reports ``{}``.

        Attributes:
            endpoint (None | str | Unset):
            preload_diagnostics (FailureDiagnostics | None | Unset):
     """

    endpoint: None | str | Unset = UNSET
    preload_diagnostics: FailureDiagnostics | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        endpoint: None | str | Unset
        if isinstance(self.endpoint, Unset):
            endpoint = UNSET
        else:
            endpoint = self.endpoint

        preload_diagnostics: dict[str, Any] | None | Unset
        if isinstance(self.preload_diagnostics, Unset):
            preload_diagnostics = UNSET
        elif isinstance(self.preload_diagnostics, FailureDiagnostics):
            preload_diagnostics = self.preload_diagnostics.to_dict()
        else:
            preload_diagnostics = self.preload_diagnostics


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if endpoint is not UNSET:
            field_dict["endpoint"] = endpoint
        if preload_diagnostics is not UNSET:
            field_dict["preload_diagnostics"] = preload_diagnostics

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        d = dict(src_dict)
        def _parse_endpoint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        endpoint = _parse_endpoint(d.pop("endpoint", UNSET))


        def _parse_preload_diagnostics(data: object) -> FailureDiagnostics | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preload_diagnostics_type_0 = FailureDiagnostics.from_dict(data)



                return preload_diagnostics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FailureDiagnostics | None | Unset, data)

        preload_diagnostics = _parse_preload_diagnostics(d.pop("preload_diagnostics", UNSET))


        recipe_start_result = cls(
            endpoint=endpoint,
            preload_diagnostics=preload_diagnostics,
        )

        return recipe_start_result
