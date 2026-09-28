from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.artifact_storage_impact_nas_coverage import ArtifactStorageImpactNasCoverage
from ..models.artifact_storage_impact_nas_coverage import check_artifact_storage_impact_nas_coverage
from ..models.artifact_storage_impact_retention import ArtifactStorageImpactRetention
from ..models.artifact_storage_impact_retention import check_artifact_storage_impact_retention
from ..models.artifact_storage_impact_running_coverage import ArtifactStorageImpactRunningCoverage
from ..models.artifact_storage_impact_running_coverage import check_artifact_storage_impact_running_coverage
from ..models.artifact_storage_impact_spark_coverage import ArtifactStorageImpactSparkCoverage
from ..models.artifact_storage_impact_spark_coverage import check_artifact_storage_impact_spark_coverage
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ArtifactStorageImpact")



@_attrs_define
class ArtifactStorageImpact:
    """ Byte impact with unknown values preserved as unknown, never guessed.

        Attributes:
            nas_coverage (ArtifactStorageImpactNasCoverage):
            retention (ArtifactStorageImpactRetention):
            spark_coverage (ArtifactStorageImpactSparkCoverage):
            artifact_digests (list[str] | Unset):
            artifact_set_bytes (int | None | Unset):
            artifact_set_sha256 (None | str | Unset):
            copied_bytes (int | Unset):  Default: 0.
            missing_nas_bytes (int | None | Unset):
            missing_spark_bytes (int | None | Unset):
            reclaimable_bytes (int | Unset):  Default: 0.
            reclaimable_digests (list[str] | Unset):
            reclaimed_bytes (int | Unset):  Default: 0.
            required_bytes (int | None | Unset):
            reused_bytes (int | Unset):  Default: 0.
            running_coverage (ArtifactStorageImpactRunningCoverage | Unset):  Default: 'unknown'.
     """

    nas_coverage: ArtifactStorageImpactNasCoverage
    retention: ArtifactStorageImpactRetention
    spark_coverage: ArtifactStorageImpactSparkCoverage
    artifact_digests: list[str] | Unset = UNSET
    artifact_set_bytes: int | None | Unset = UNSET
    artifact_set_sha256: None | str | Unset = UNSET
    copied_bytes: int | Unset = 0
    missing_nas_bytes: int | None | Unset = UNSET
    missing_spark_bytes: int | None | Unset = UNSET
    reclaimable_bytes: int | Unset = 0
    reclaimable_digests: list[str] | Unset = UNSET
    reclaimed_bytes: int | Unset = 0
    required_bytes: int | None | Unset = UNSET
    reused_bytes: int | Unset = 0
    running_coverage: ArtifactStorageImpactRunningCoverage | Unset = 'unknown'





    def to_dict(self) -> dict[str, Any]:
        nas_coverage: str = self.nas_coverage

        retention: str = self.retention

        spark_coverage: str = self.spark_coverage

        artifact_digests: list[str] | Unset = UNSET
        if not isinstance(self.artifact_digests, Unset):
            artifact_digests = self.artifact_digests



        artifact_set_bytes: int | None | Unset
        if isinstance(self.artifact_set_bytes, Unset):
            artifact_set_bytes = UNSET
        else:
            artifact_set_bytes = self.artifact_set_bytes

        artifact_set_sha256: None | str | Unset
        if isinstance(self.artifact_set_sha256, Unset):
            artifact_set_sha256 = UNSET
        else:
            artifact_set_sha256 = self.artifact_set_sha256

        copied_bytes = self.copied_bytes

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

        reclaimable_bytes = self.reclaimable_bytes

        reclaimable_digests: list[str] | Unset = UNSET
        if not isinstance(self.reclaimable_digests, Unset):
            reclaimable_digests = self.reclaimable_digests



        reclaimed_bytes = self.reclaimed_bytes

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
            "nas_coverage": nas_coverage,
            "retention": retention,
            "spark_coverage": spark_coverage,
        })
        if artifact_digests is not UNSET:
            field_dict["artifact_digests"] = artifact_digests
        if artifact_set_bytes is not UNSET:
            field_dict["artifact_set_bytes"] = artifact_set_bytes
        if artifact_set_sha256 is not UNSET:
            field_dict["artifact_set_sha256"] = artifact_set_sha256
        if copied_bytes is not UNSET:
            field_dict["copied_bytes"] = copied_bytes
        if missing_nas_bytes is not UNSET:
            field_dict["missing_nas_bytes"] = missing_nas_bytes
        if missing_spark_bytes is not UNSET:
            field_dict["missing_spark_bytes"] = missing_spark_bytes
        if reclaimable_bytes is not UNSET:
            field_dict["reclaimable_bytes"] = reclaimable_bytes
        if reclaimable_digests is not UNSET:
            field_dict["reclaimable_digests"] = reclaimable_digests
        if reclaimed_bytes is not UNSET:
            field_dict["reclaimed_bytes"] = reclaimed_bytes
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
        nas_coverage = check_artifact_storage_impact_nas_coverage(d.pop("nas_coverage"))




        retention = check_artifact_storage_impact_retention(d.pop("retention"))




        spark_coverage = check_artifact_storage_impact_spark_coverage(d.pop("spark_coverage"))




        artifact_digests = cast(list[str], d.pop("artifact_digests", UNSET))


        def _parse_artifact_set_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        artifact_set_bytes = _parse_artifact_set_bytes(d.pop("artifact_set_bytes", UNSET))


        def _parse_artifact_set_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256", UNSET))


        copied_bytes = d.pop("copied_bytes", UNSET)

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


        reclaimable_bytes = d.pop("reclaimable_bytes", UNSET)

        reclaimable_digests = cast(list[str], d.pop("reclaimable_digests", UNSET))


        reclaimed_bytes = d.pop("reclaimed_bytes", UNSET)

        def _parse_required_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        required_bytes = _parse_required_bytes(d.pop("required_bytes", UNSET))


        reused_bytes = d.pop("reused_bytes", UNSET)

        _running_coverage = d.pop("running_coverage", UNSET)
        running_coverage: ArtifactStorageImpactRunningCoverage | Unset
        if isinstance(_running_coverage,  Unset):
            running_coverage = UNSET
        else:
            running_coverage = check_artifact_storage_impact_running_coverage(_running_coverage)




        artifact_storage_impact = cls(
            nas_coverage=nas_coverage,
            retention=retention,
            spark_coverage=spark_coverage,
            artifact_digests=artifact_digests,
            artifact_set_bytes=artifact_set_bytes,
            artifact_set_sha256=artifact_set_sha256,
            copied_bytes=copied_bytes,
            missing_nas_bytes=missing_nas_bytes,
            missing_spark_bytes=missing_spark_bytes,
            reclaimable_bytes=reclaimable_bytes,
            reclaimable_digests=reclaimable_digests,
            reclaimed_bytes=reclaimed_bytes,
            required_bytes=required_bytes,
            reused_bytes=reused_bytes,
            running_coverage=running_coverage,
        )

        return artifact_storage_impact
