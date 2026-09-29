from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

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






T = TypeVar("T", bound="RuntimeImageStorageImpact")



@_attrs_define
class RuntimeImageStorageImpact:
    """
        Attributes:
            build_id (None | str):
            image_digest (None | str):
            nas_coverage (RuntimeImageStorageImpactNasCoverage):
            preparation_required (bool):
            spark_coverage (RuntimeImageStorageImpactSparkCoverage):
            copied_bytes (int | Unset):  Default: 0.
            image_bytes (int | None | Unset):
            missing_image_distribution_bytes (int | None | Unset):
            missing_nas_bytes (int | None | Unset):
            missing_spark_bytes (int | None | Unset):
            oci_layout_sha256 (None | str | Unset):
            reclaimable_bytes (int | Unset):  Default: 0.
            reclaimable_digests (list[str] | Unset):
            required_bytes (int | None | Unset):
            reused_bytes (int | Unset):  Default: 0.
            running_coverage (RuntimeImageStorageImpactRunningCoverage | Unset):  Default: 'unknown'.
     """

    build_id: None | str
    image_digest: None | str
    nas_coverage: RuntimeImageStorageImpactNasCoverage
    preparation_required: bool
    spark_coverage: RuntimeImageStorageImpactSparkCoverage
    copied_bytes: int | Unset = 0
    image_bytes: int | None | Unset = UNSET
    missing_image_distribution_bytes: int | None | Unset = UNSET
    missing_nas_bytes: int | None | Unset = UNSET
    missing_spark_bytes: int | None | Unset = UNSET
    oci_layout_sha256: None | str | Unset = UNSET
    reclaimable_bytes: int | Unset = 0
    reclaimable_digests: list[str] | Unset = UNSET
    required_bytes: int | None | Unset = UNSET
    reused_bytes: int | Unset = 0
    running_coverage: RuntimeImageStorageImpactRunningCoverage | Unset = 'unknown'





    def to_dict(self) -> dict[str, Any]:
        build_id: None | str
        build_id = self.build_id

        image_digest: None | str
        image_digest = self.image_digest

        nas_coverage: str = self.nas_coverage

        preparation_required = self.preparation_required

        spark_coverage: str = self.spark_coverage

        copied_bytes = self.copied_bytes

        image_bytes: int | None | Unset
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        missing_image_distribution_bytes: int | None | Unset
        if isinstance(self.missing_image_distribution_bytes, Unset):
            missing_image_distribution_bytes = UNSET
        else:
            missing_image_distribution_bytes = self.missing_image_distribution_bytes

        missing_nas_bytes: int | None | Unset
        if isinstance(self.missing_nas_bytes, Unset):
            missing_nas_bytes = UNSET
        else:
            missing_nas_bytes = self.missing_nas_bytes

        missing_spark_bytes: int | None | Unset
        if isinstance(self.missing_spark_bytes, Unset):
            missing_spark_bytes = UNSET
        else:
            missing_spark_bytes = self.missing_spark_bytes

        oci_layout_sha256: None | str | Unset
        if isinstance(self.oci_layout_sha256, Unset):
            oci_layout_sha256 = UNSET
        else:
            oci_layout_sha256 = self.oci_layout_sha256

        reclaimable_bytes = self.reclaimable_bytes

        reclaimable_digests: list[str] | Unset = UNSET
        if not isinstance(self.reclaimable_digests, Unset):
            reclaimable_digests = self.reclaimable_digests



        required_bytes: int | None | Unset
        if isinstance(self.required_bytes, Unset):
            required_bytes = UNSET
        else:
            required_bytes = self.required_bytes

        reused_bytes = self.reused_bytes

        running_coverage: str | Unset = UNSET
        if not isinstance(self.running_coverage, Unset):
            running_coverage = self.running_coverage



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "image_digest": image_digest,
            "nas_coverage": nas_coverage,
            "preparation_required": preparation_required,
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
        if oci_layout_sha256 is not UNSET:
            field_dict["oci_layout_sha256"] = oci_layout_sha256
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
        def _parse_build_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        build_id = _parse_build_id(d.pop("build_id"))


        def _parse_image_digest(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        image_digest = _parse_image_digest(d.pop("image_digest"))


        nas_coverage = check_runtime_image_storage_impact_nas_coverage(d.pop("nas_coverage"))




        preparation_required = d.pop("preparation_required")

        spark_coverage = check_runtime_image_storage_impact_spark_coverage(d.pop("spark_coverage"))




        copied_bytes = d.pop("copied_bytes", UNSET)

        def _parse_image_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_missing_image_distribution_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        missing_image_distribution_bytes = _parse_missing_image_distribution_bytes(d.pop("missing_image_distribution_bytes", UNSET))


        def _parse_missing_nas_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        missing_nas_bytes = _parse_missing_nas_bytes(d.pop("missing_nas_bytes", UNSET))


        def _parse_missing_spark_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        missing_spark_bytes = _parse_missing_spark_bytes(d.pop("missing_spark_bytes", UNSET))


        def _parse_oci_layout_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        oci_layout_sha256 = _parse_oci_layout_sha256(d.pop("oci_layout_sha256", UNSET))


        reclaimable_bytes = d.pop("reclaimable_bytes", UNSET)

        reclaimable_digests = cast(list[str], d.pop("reclaimable_digests", UNSET))


        def _parse_required_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        required_bytes = _parse_required_bytes(d.pop("required_bytes", UNSET))


        reused_bytes = d.pop("reused_bytes", UNSET)

        _running_coverage = d.pop("running_coverage", UNSET)
        running_coverage: RuntimeImageStorageImpactRunningCoverage | Unset
        if isinstance(_running_coverage,  Unset):
            running_coverage = UNSET
        else:
            running_coverage = check_runtime_image_storage_impact_running_coverage(_running_coverage)




        runtime_image_storage_impact = cls(
            build_id=build_id,
            image_digest=image_digest,
            nas_coverage=nas_coverage,
            preparation_required=preparation_required,
            spark_coverage=spark_coverage,
            copied_bytes=copied_bytes,
            image_bytes=image_bytes,
            missing_image_distribution_bytes=missing_image_distribution_bytes,
            missing_nas_bytes=missing_nas_bytes,
            missing_spark_bytes=missing_spark_bytes,
            oci_layout_sha256=oci_layout_sha256,
            reclaimable_bytes=reclaimable_bytes,
            reclaimable_digests=reclaimable_digests,
            required_bytes=required_bytes,
            reused_bytes=reused_bytes,
            running_coverage=running_coverage,
        )

        return runtime_image_storage_impact
