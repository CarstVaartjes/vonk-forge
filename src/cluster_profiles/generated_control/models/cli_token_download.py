from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CliTokenDownload")



@_attrs_define
class CliTokenDownload:
    """ What the browser learns from a token download: when the token stops working.

    The token itself is the response body, exact bytes; the expiry travels in the
    ``X-Vonk-Token-Expires-At`` header, which the web client reads into this shape.

        Attributes:
            expires_at (str):
     """

    expires_at: str





    def to_dict(self) -> dict[str, Any]:
        expires_at = self.expires_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expires_at": expires_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expires_at = d.pop("expires_at")

        cli_token_download = cls(
            expires_at=expires_at,
        )

        return cli_token_download
