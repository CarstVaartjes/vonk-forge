from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.package_rollback_source import PackageRollbackSource





T = TypeVar("T", bound="PackageRollbackAuthority")



@_attrs_define
class PackageRollbackAuthority:
    """
        Attributes:
            activation_deadline (int):
            attempt_nonce (str):
            source (PackageRollbackSource):
     """

    activation_deadline: int
    attempt_nonce: str
    source: PackageRollbackSource





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_rollback_source import PackageRollbackSource # noqa: PLC0415
        activation_deadline = self.activation_deadline

        attempt_nonce = self.attempt_nonce

        source = self.source.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "activation_deadline": activation_deadline,
            "attempt_nonce": attempt_nonce,
            "source": source,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_rollback_source import PackageRollbackSource # noqa: PLC0415
        d = dict(src_dict)
        activation_deadline = d.pop("activation_deadline")

        attempt_nonce = d.pop("attempt_nonce")

        source = PackageRollbackSource.from_dict(d.pop("source"))




        package_rollback_authority = cls(
            activation_deadline=activation_deadline,
            attempt_nonce=attempt_nonce,
            source=source,
        )

        return package_rollback_authority
