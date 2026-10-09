from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.transient_reason import check_transient_reason
from ..models.transient_reason import TransientReason
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.renewal_window import RenewalWindow





T = TypeVar("T", bound="HttpTransient")



@_attrs_define
class HttpTransient:
    """
        Attributes:
            family (Literal['transient']):
            reason (TransientReason):
            resolution_window (int):
            retry_after (int):
            suggested_window (None | RenewalWindow | Unset):
     """

    family: Literal['transient']
    reason: TransientReason
    resolution_window: int
    retry_after: int
    suggested_window: None | RenewalWindow | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.renewal_window import RenewalWindow # noqa: PLC0415
        family = self.family

        reason: str = self.reason

        resolution_window = self.resolution_window

        retry_after = self.retry_after

        suggested_window: dict[str, Any] | None | Unset
        if isinstance(self.suggested_window, Unset):
            suggested_window = UNSET
        elif isinstance(self.suggested_window, RenewalWindow):
            suggested_window = self.suggested_window.to_dict()
        else:
            suggested_window = self.suggested_window


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "family": family,
            "reason": reason,
            "resolution_window": resolution_window,
            "retry_after": retry_after,
        })
        if suggested_window is not UNSET:
            field_dict["suggested_window"] = suggested_window

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.renewal_window import RenewalWindow # noqa: PLC0415
        d = dict(src_dict)
        family = cast(Literal['transient'] , d.pop("family"))
        if family != 'transient':
            raise ValueError(f"family must match const 'transient', got '{family}'")

        reason = check_transient_reason(d.pop("reason"))




        resolution_window = d.pop("resolution_window")

        retry_after = d.pop("retry_after")

        def _parse_suggested_window(data: object) -> None | RenewalWindow | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                suggested_window_type_0 = RenewalWindow.from_dict(data)



                return suggested_window_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RenewalWindow | Unset, data)

        suggested_window = _parse_suggested_window(d.pop("suggested_window", UNSET))


        http_transient = cls(
            family=family,
            reason=reason,
            resolution_window=resolution_window,
            retry_after=retry_after,
            suggested_window=suggested_window,
        )

        return http_transient
