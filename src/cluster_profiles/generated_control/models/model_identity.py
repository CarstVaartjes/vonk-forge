from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.model_family import ModelFamily
  from ..models.model_record import ModelRecord





T = TypeVar("T", bound="ModelIdentity")



@_attrs_define
class ModelIdentity:
    """ The family, logical model, exact version, and selected variant.

        Attributes:
            family (ModelFamily):
            model (ModelRecord):
            publisher (str):
            slug (str):
            variant (str):
            version (str):
     """

    family: 'ModelFamily'
    model: 'ModelRecord'
    publisher: str
    slug: str
    variant: str
    version: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_family import ModelFamily
        from ..models.model_record import ModelRecord
        family = self.family.to_dict()

        model = self.model.to_dict()

        publisher = self.publisher

        slug = self.slug

        variant = self.variant

        version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "family": family,
            "model": model,
            "publisher": publisher,
            "slug": slug,
            "variant": variant,
            "version": version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_family import ModelFamily
        from ..models.model_record import ModelRecord
        d = dict(src_dict)
        family = ModelFamily.from_dict(d.pop("family"))




        model = ModelRecord.from_dict(d.pop("model"))




        publisher = d.pop("publisher")

        slug = d.pop("slug")

        variant = d.pop("variant")

        version = d.pop("version")

        model_identity = cls(
            family=family,
            model=model,
            publisher=publisher,
            slug=slug,
            variant=variant,
            version=version,
        )

        return model_identity
