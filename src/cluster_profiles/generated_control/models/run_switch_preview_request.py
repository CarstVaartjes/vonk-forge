from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_preview_request_action import check_run_switch_preview_request_action
from ..models.run_switch_preview_request_action import RunSwitchPreviewRequestAction
from ..models.run_switch_preview_request_retention import check_run_switch_preview_request_retention
from ..models.run_switch_preview_request_retention import RunSwitchPreviewRequestRetention
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.invocation_metadata import InvocationMetadata
  from ..models.spark_group import SparkGroup





T = TypeVar("T", bound="RunSwitchPreviewRequest")



@_attrs_define
class RunSwitchPreviewRequest:
    """
        Attributes:
            alias (str):
            model_version_sha256 (str):
            recipe_revision_id (str):
            spark_group (SparkGroup): A complete, rank-labelled Spark group selected by the operator.
            action (Union[Unset, RunSwitchPreviewRequestAction]):  Default: 'run'.
            invocation (Union[Unset, InvocationMetadata]): Context for audit and tracing which has no decision-making
                authority.
            retention (Union[Unset, RunSwitchPreviewRequestRetention]):  Default: 'retain-cached'.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    alias: str
    model_version_sha256: str
    recipe_revision_id: str
    spark_group: 'SparkGroup'
    action: Union[Unset, RunSwitchPreviewRequestAction] = 'run'
    invocation: Union[Unset, 'InvocationMetadata'] = UNSET
    retention: Union[Unset, RunSwitchPreviewRequestRetention] = 'retain-cached'
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.invocation_metadata import InvocationMetadata
        from ..models.spark_group import SparkGroup
        alias = self.alias

        model_version_sha256 = self.model_version_sha256

        recipe_revision_id = self.recipe_revision_id

        spark_group = self.spark_group.to_dict()

        action: Union[Unset, str] = UNSET
        if not isinstance(self.action, Unset):
            action = self.action


        invocation: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.invocation, Unset):
            invocation = self.invocation.to_dict()

        retention: Union[Unset, str] = UNSET
        if not isinstance(self.retention, Unset):
            retention = self.retention


        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "model_version_sha256": model_version_sha256,
            "recipe_revision_id": recipe_revision_id,
            "spark_group": spark_group,
        })
        if action is not UNSET:
            field_dict["action"] = action
        if invocation is not UNSET:
            field_dict["invocation"] = invocation
        if retention is not UNSET:
            field_dict["retention"] = retention
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invocation_metadata import InvocationMetadata
        from ..models.spark_group import SparkGroup
        d = dict(src_dict)
        alias = d.pop("alias")

        model_version_sha256 = d.pop("model_version_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        spark_group = SparkGroup.from_dict(d.pop("spark_group"))




        _action = d.pop("action", UNSET)
        action: Union[Unset, RunSwitchPreviewRequestAction]
        if isinstance(_action,  Unset):
            action = UNSET
        else:
            action = check_run_switch_preview_request_action(_action)




        _invocation = d.pop("invocation", UNSET)
        invocation: Union[Unset, InvocationMetadata]
        if isinstance(_invocation,  Unset):
            invocation = UNSET
        else:
            invocation = InvocationMetadata.from_dict(_invocation)




        _retention = d.pop("retention", UNSET)
        retention: Union[Unset, RunSwitchPreviewRequestRetention]
        if isinstance(_retention,  Unset):
            retention = UNSET
        else:
            retention = check_run_switch_preview_request_retention(_retention)




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_preview_request = cls(
            alias=alias,
            model_version_sha256=model_version_sha256,
            recipe_revision_id=recipe_revision_id,
            spark_group=spark_group,
            action=action,
            invocation=invocation,
            retention=retention,
            schema_version=schema_version,
        )

        return run_switch_preview_request
