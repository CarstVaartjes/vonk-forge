from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_apply_request_action import check_run_switch_apply_request_action
from ..models.run_switch_apply_request_action import RunSwitchApplyRequestAction
from ..models.run_switch_apply_request_retention import check_run_switch_apply_request_retention
from ..models.run_switch_apply_request_retention import RunSwitchApplyRequestRetention
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.invocation_metadata import InvocationMetadata
  from ..models.spark_group import SparkGroup





T = TypeVar("T", bound="RunSwitchApplyRequest")



@_attrs_define
class RunSwitchApplyRequest:
    """
        Attributes:
            alias (str):
            model_version_sha256 (str):
            recipe_revision_id (str):
            spark_group (SparkGroup): A complete, rank-labelled Spark group selected by the operator.
            action (Union[Unset, RunSwitchApplyRequestAction]):  Default: 'run'.
            invocation (Union[Unset, InvocationMetadata]): Context for audit and tracing which has no decision-making
                authority.
            plan_digest (Union[None, Unset, str]):
            request_key (Union[None, Unset, str]):
            retention (Union[Unset, RunSwitchApplyRequestRetention]):  Default: 'retain-cached'.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    alias: str
    model_version_sha256: str
    recipe_revision_id: str
    spark_group: 'SparkGroup'
    action: Union[Unset, RunSwitchApplyRequestAction] = 'run'
    invocation: Union[Unset, 'InvocationMetadata'] = UNSET
    plan_digest: Union[None, Unset, str] = UNSET
    request_key: Union[None, Unset, str] = UNSET
    retention: Union[Unset, RunSwitchApplyRequestRetention] = 'retain-cached'
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

        plan_digest: Union[None, Unset, str]
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        request_key: Union[None, Unset, str]
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key

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
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if request_key is not UNSET:
            field_dict["request_key"] = request_key
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
        action: Union[Unset, RunSwitchApplyRequestAction]
        if isinstance(_action,  Unset):
            action = UNSET
        else:
            action = check_run_switch_apply_request_action(_action)




        _invocation = d.pop("invocation", UNSET)
        invocation: Union[Unset, InvocationMetadata]
        if isinstance(_invocation,  Unset):
            invocation = UNSET
        else:
            invocation = InvocationMetadata.from_dict(_invocation)




        def _parse_plan_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_request_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        _retention = d.pop("retention", UNSET)
        retention: Union[Unset, RunSwitchApplyRequestRetention]
        if isinstance(_retention,  Unset):
            retention = UNSET
        else:
            retention = check_run_switch_apply_request_retention(_retention)




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_apply_request = cls(
            alias=alias,
            model_version_sha256=model_version_sha256,
            recipe_revision_id=recipe_revision_id,
            spark_group=spark_group,
            action=action,
            invocation=invocation,
            plan_digest=plan_digest,
            request_key=request_key,
            retention=retention,
            schema_version=schema_version,
        )

        return run_switch_apply_request
