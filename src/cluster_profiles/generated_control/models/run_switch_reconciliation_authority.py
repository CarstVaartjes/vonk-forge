from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.run_switch_reconciliation_target import RunSwitchReconciliationTarget





T = TypeVar("T", bound="RunSwitchReconciliationAuthority")



@_attrs_define
class RunSwitchReconciliationAuthority:
    """ Controller-owned identity and effect binding for installation repair.

    The accepted installation plan remains opaque.  This authority records its
    canonical fingerprint and binds each target to the successful original
    ``recipe.install`` operation that supplied the persisted compiled spec.
    It never claims that malformed launch metadata is executable.

        Attributes:
            image_digest (str):
            installation_id (str):
            mapping_generation (int):
            mapping_id (str):
            model_content_sha256 (Union[None, str]):
            original_plan_digest (str):
            recipe_build_id (Union[None, str]):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            stored_plan_canonical_sha256 (str):
            targets (list['RunSwitchReconciliationTarget']):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    image_digest: str
    installation_id: str
    mapping_generation: int
    mapping_id: str
    model_content_sha256: Union[None, str]
    original_plan_digest: str
    recipe_build_id: Union[None, str]
    recipe_content_sha256: str
    recipe_revision_id: str
    stored_plan_canonical_sha256: str
    targets: list['RunSwitchReconciliationTarget']
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_reconciliation_target import RunSwitchReconciliationTarget
        image_digest = self.image_digest

        installation_id = self.installation_id

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        model_content_sha256: Union[None, str]
        model_content_sha256 = self.model_content_sha256

        original_plan_digest = self.original_plan_digest

        recipe_build_id: Union[None, str]
        recipe_build_id = self.recipe_build_id

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        stored_plan_canonical_sha256 = self.stored_plan_canonical_sha256

        targets = []
        for targets_item_data in self.targets:
            targets_item = targets_item_data.to_dict()
            targets.append(targets_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_digest": image_digest,
            "installation_id": installation_id,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "model_content_sha256": model_content_sha256,
            "original_plan_digest": original_plan_digest,
            "recipe_build_id": recipe_build_id,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "stored_plan_canonical_sha256": stored_plan_canonical_sha256,
            "targets": targets,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_reconciliation_target import RunSwitchReconciliationTarget
        d = dict(src_dict)
        image_digest = d.pop("image_digest")

        installation_id = d.pop("installation_id")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        def _parse_model_content_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256"))


        original_plan_digest = d.pop("original_plan_digest")

        def _parse_recipe_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_build_id = _parse_recipe_build_id(d.pop("recipe_build_id"))


        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        stored_plan_canonical_sha256 = d.pop("stored_plan_canonical_sha256")

        targets = []
        _targets = d.pop("targets")
        for targets_item_data in (_targets):
            targets_item = RunSwitchReconciliationTarget.from_dict(targets_item_data)



            targets.append(targets_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_reconciliation_authority = cls(
            image_digest=image_digest,
            installation_id=installation_id,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            model_content_sha256=model_content_sha256,
            original_plan_digest=original_plan_digest,
            recipe_build_id=recipe_build_id,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            stored_plan_canonical_sha256=stored_plan_canonical_sha256,
            targets=targets,
            schema_version=schema_version,
        )

        return run_switch_reconciliation_authority
