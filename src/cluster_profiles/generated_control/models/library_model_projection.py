from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.model_definition import ModelDefinition
  from ..models.library_local_state import LibraryLocalState
  from ..models.library_model_identity import LibraryModelIdentity
  from ..models.library_resource_projection import LibraryResourceProjection





T = TypeVar("T", bound="LibraryModelProjection")



@_attrs_define
class LibraryModelProjection:
    """ One exact canonical model variant with operator-facing projections.

        Attributes:
            document (ModelDefinition): One exact model version and variant, including its complete manifest.
            family (str):
            identity (LibraryModelIdentity): Content-addressed identity for a canonical Model document.
            local (LibraryLocalState): Controller cache and Spark-local runtime evidence kept separate.
            quantization (str):
            resources (LibraryResourceProjection): Declared resource facts; unknown values remain null.
            selector (str):
            updated_at (datetime.datetime):
            usage (list[str]):
            variant (str):
            version (str):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    document: 'ModelDefinition'
    family: str
    identity: 'LibraryModelIdentity'
    local: 'LibraryLocalState'
    quantization: str
    resources: 'LibraryResourceProjection'
    selector: str
    updated_at: datetime.datetime
    usage: list[str]
    variant: str
    version: str
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_definition import ModelDefinition
        from ..models.library_local_state import LibraryLocalState
        from ..models.library_model_identity import LibraryModelIdentity
        from ..models.library_resource_projection import LibraryResourceProjection
        document = self.document.to_dict()

        family = self.family

        identity = self.identity.to_dict()

        local = self.local.to_dict()

        quantization = self.quantization

        resources = self.resources.to_dict()

        selector = self.selector

        updated_at = self.updated_at.isoformat()

        usage = self.usage



        variant = self.variant

        version = self.version

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "family": family,
            "identity": identity,
            "local": local,
            "quantization": quantization,
            "resources": resources,
            "selector": selector,
            "updated_at": updated_at,
            "usage": usage,
            "variant": variant,
            "version": version,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_definition import ModelDefinition
        from ..models.library_local_state import LibraryLocalState
        from ..models.library_model_identity import LibraryModelIdentity
        from ..models.library_resource_projection import LibraryResourceProjection
        d = dict(src_dict)
        document = ModelDefinition.from_dict(d.pop("document"))




        family = d.pop("family")

        identity = LibraryModelIdentity.from_dict(d.pop("identity"))




        local = LibraryLocalState.from_dict(d.pop("local"))




        quantization = d.pop("quantization")

        resources = LibraryResourceProjection.from_dict(d.pop("resources"))




        selector = d.pop("selector")

        updated_at = isoparse(d.pop("updated_at"))




        usage = cast(list[str], d.pop("usage"))


        variant = d.pop("variant")

        version = d.pop("version")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        library_model_projection = cls(
            document=document,
            family=family,
            identity=identity,
            local=local,
            quantization=quantization,
            resources=resources,
            selector=selector,
            updated_at=updated_at,
            usage=usage,
            variant=variant,
            version=version,
            schema_version=schema_version,
        )

        return library_model_projection
