from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.runtime_image_storage_impact_nas_coverage import check_runtime_image_storage_impact_nas_coverage
from ..models.runtime_image_storage_impact_nas_coverage import RuntimeImageStorageImpactNasCoverage
from ..models.runtime_image_storage_impact_running_coverage import check_runtime_image_storage_impact_running_coverage
from ..models.runtime_image_storage_impact_running_coverage import RuntimeImageStorageImpactRunningCoverage
from ..models.runtime_image_storage_impact_spark_coverage import check_runtime_image_storage_impact_spark_coverage
from ..models.runtime_image_storage_impact_spark_coverage import RuntimeImageStorageImpactSparkCoverage
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RuntimeImageStorageImpact")



@_attrs_define
class RuntimeImageStorageImpact:
    """
        Attributes:
            build_id (Union[None, str]):
            image_digest (Union[None, str]):
            nas_coverage (RuntimeImageStorageImpactNasCoverage):
            spark_coverage (RuntimeImageStorageImpactSparkCoverage):
            copied_bytes (Union[Unset, int]):  Default: 0.
            image_bytes (Union[None, Unset, int]):
            missing_image_distribution_bytes (Union[None, Unset, int]):
            missing_nas_bytes (Union[None, Unset, int]):
            missing_spark_bytes (Union[None, Unset, int]):
            reclaimable_bytes (Union[Unset, int]):  Default: 0.
            reclaimable_digests (Union[Unset, list[str]]):
            required_bytes (Union[None, Unset, int]):
            reused_bytes (Union[Unset, int]):  Default: 0.
            running_coverage (Union[Unset, RuntimeImageStorageImpactRunningCoverage]):  Default: 'unknown'.
     """

    build_id: Union[None, str]
    image_digest: Union[None, str]
    nas_coverage: RuntimeImageStorageImpactNasCoverage
    spark_coverage: RuntimeImageStorageImpactSparkCoverage
    copied_bytes: Union[Unset, int] = 0
    image_bytes: Union[None, Unset, int] = UNSET
    missing_image_distribution_bytes: Union[None, Unset, int] = UNSET
    missing_nas_bytes: Union[None, Unset, int] = UNSET
    missing_spark_bytes: Union[None, Unset, int] = UNSET
    reclaimable_bytes: Union[Unset, int] = 0
    reclaimable_digests: Union[Unset, list[str]] = UNSET
    required_bytes: Union[None, Unset, int] = UNSET
    reused_bytes: Union[Unset, int] = 0
    running_coverage: Union[Unset, RuntimeImageStorageImpactRunningCoverage] = 'unknown'





    def to_dict(self) -> dict[str, Any]:
        build_id: Union[None, str]
        build_id = self.build_id

        image_digest: Union[None, str]
        image_digest = self.image_digest

        nas_coverage: str = self.nas_coverage

        spark_coverage: str = self.spark_coverage

        copied_bytes = self.copied_bytes

        image_bytes: Union[None, Unset, int]
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        missing_image_distribution_bytes: Union[None, Unset, int]
        if isinstance(self.missing_image_distribution_bytes, Unset):
            missing_image_distribution_bytes = UNSET
        else:
            missing_image_distribution_bytes = self.missing_image_distribution_bytes

        missing_nas_bytes: Union[None, Unset, int]
        if isinstance(self.missing_nas_bytes, Unset):
            missing_nas_bytes = UNSET
        else:
            missing_nas_bytes = self.missing_nas_bytes

        missing_spark_bytes: Union[None, Unset, int]
        if isinstance(self.missing_spark_bytes, Unset):
            missing_spark_bytes = UNSET
        else:
            missing_spark_bytes = self.missing_spark_bytes

        reclaimable_bytes = self.reclaimable_bytes

        reclaimable_digests: Union[Unset, list[str]] = UNSET
        if not isinstance(self.reclaimable_digests, Unset):
            reclaimable_digests = self.reclaimable_digests



        required_bytes: Union[None, Unset, int]
        if isinstance(self.required_bytes, Unset):
            required_bytes = UNSET
        else:
            required_bytes = self.required_bytes

        reused_bytes = self.reused_bytes

        running_coverage: Union[Unset, str] = UNSET
        if not isinstance(self.running_coverage, Unset):
            running_coverage = self.running_coverage



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "image_digest": image_digest,
            "nas_coverage": nas_coverage,
            "spark_coverage": spark_coverage,
        })
        if copied_bytes is not UNSET:
            field_dict["copied_bytes"] = copied_bytes
        if image_bytes is not UNSET:
            field_dict["image_bytes"] = image_bytes
        if missing_image_distribution_bytes is not UNSET:
            field_dict["missing_image_distribution_bytes"] = missing_image_distribution_bytes
        if missing_nas_bytes is not UNSET:
            field_dict["missing_nas_bytes"] = missing_nas_bytes
        if missing_spark_bytes is not UNSET:
            field_dict["missing_spark_bytes"] = missing_spark_bytes
        if reclaimable_bytes is not UNSET:
            field_dict["reclaimable_bytes"] = reclaimable_bytes
        if reclaimable_digests is not UNSET:
            field_dict["reclaimable_digests"] = reclaimable_digests
        if required_bytes is not UNSET:
            field_dict["required_bytes"] = required_bytes
        if reused_bytes is not UNSET:
            field_dict["reused_bytes"] = reused_bytes
        if running_coverage is not UNSET:
            field_dict["running_coverage"] = running_coverage

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        build_id = _parse_build_id(d.pop("build_id"))


        def _parse_image_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        image_digest = _parse_image_digest(d.pop("image_digest"))


        nas_coverage = check_runtime_image_storage_impact_nas_coverage(d.pop("nas_coverage"))




        spark_coverage = check_runtime_image_storage_impact_spark_coverage(d.pop("spark_coverage"))




        copied_bytes = d.pop("copied_bytes", UNSET)

        def _parse_image_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_missing_image_distribution_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        missing_image_distribution_bytes = _parse_missing_image_distribution_bytes(d.pop("missing_image_distribution_bytes", UNSET))


        def _parse_missing_nas_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        missing_nas_bytes = _parse_missing_nas_bytes(d.pop("missing_nas_bytes", UNSET))


        def _parse_missing_spark_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        missing_spark_bytes = _parse_missing_spark_bytes(d.pop("missing_spark_bytes", UNSET))


        reclaimable_bytes = d.pop("reclaimable_bytes", UNSET)

        reclaimable_digests = cast(list[str], d.pop("reclaimable_digests", UNSET))


        def _parse_required_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        required_bytes = _parse_required_bytes(d.pop("required_bytes", UNSET))


        reused_bytes = d.pop("reused_bytes", UNSET)

        _running_coverage = d.pop("running_coverage", UNSET)
        running_coverage: Union[Unset, RuntimeImageStorageImpactRunningCoverage]
        if isinstance(_running_coverage,  Unset):
            running_coverage = UNSET
        else:
            running_coverage = check_runtime_image_storage_impact_running_coverage(_running_coverage)




        runtime_image_storage_impact = cls(
            build_id=build_id,
            image_digest=image_digest,
            nas_coverage=nas_coverage,
            spark_coverage=spark_coverage,
            copied_bytes=copied_bytes,
            image_bytes=image_bytes,
            missing_image_distribution_bytes=missing_image_distribution_bytes,
            missing_nas_bytes=missing_nas_bytes,
            missing_spark_bytes=missing_spark_bytes,
            reclaimable_bytes=reclaimable_bytes,
            reclaimable_digests=reclaimable_digests,
            required_bytes=required_bytes,
            reused_bytes=reused_bytes,
            running_coverage=running_coverage,
        )

        return runtime_image_storage_impact
