from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_package_handle_projection import RecipePackageHandleProjection





T = TypeVar("T", bound="UnprojectedRevision")



@_attrs_define
class UnprojectedRevision:
    """ The column's own default: a revision whose projection is not derived yet.

    ``catalog_document_revisions.projected`` defaults to ``{}``.  The importer
    always writes the derived projection; a row that still holds the default is
    read as unknown by :func:`read_catalog_projection` and re-derived by the
    catalog's own repair path.

        Attributes:
            failure_reason (None | str | Unset):
            package_handle (None | RecipePackageHandleProjection | Unset):
            package_sha256 (None | str | Unset):
            publication_commit (None | str | Unset):
            release_released_at (None | str | Unset):
            release_version (None | str | Unset):
            source_bundle_sha256 (None | str | Unset):
            source_path (None | str | Unset):
     """

    failure_reason: None | str | Unset = UNSET
    package_handle: None | RecipePackageHandleProjection | Unset = UNSET
    package_sha256: None | str | Unset = UNSET
    publication_commit: None | str | Unset = UNSET
    release_released_at: None | str | Unset = UNSET
    release_version: None | str | Unset = UNSET
    source_bundle_sha256: None | str | Unset = UNSET
    source_path: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_package_handle_projection import RecipePackageHandleProjection # noqa: PLC0415
        failure_reason: None | str | Unset
        if isinstance(self.failure_reason, Unset):
            failure_reason = UNSET
        else:
            failure_reason = self.failure_reason

        package_handle: dict[str, Any] | None | Unset
        if isinstance(self.package_handle, Unset):
            package_handle = UNSET
        elif isinstance(self.package_handle, RecipePackageHandleProjection):
            package_handle = self.package_handle.to_dict()
        else:
            package_handle = self.package_handle

        package_sha256: None | str | Unset
        if isinstance(self.package_sha256, Unset):
            package_sha256 = UNSET
        else:
            package_sha256 = self.package_sha256

        publication_commit: None | str | Unset
        if isinstance(self.publication_commit, Unset):
            publication_commit = UNSET
        else:
            publication_commit = self.publication_commit

        release_released_at: None | str | Unset
        if isinstance(self.release_released_at, Unset):
            release_released_at = UNSET
        else:
            release_released_at = self.release_released_at

        release_version: None | str | Unset
        if isinstance(self.release_version, Unset):
            release_version = UNSET
        else:
            release_version = self.release_version

        source_bundle_sha256: None | str | Unset
        if isinstance(self.source_bundle_sha256, Unset):
            source_bundle_sha256 = UNSET
        else:
            source_bundle_sha256 = self.source_bundle_sha256

        source_path: None | str | Unset
        if isinstance(self.source_path, Unset):
            source_path = UNSET
        else:
            source_path = self.source_path


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if failure_reason is not UNSET:
            field_dict["failure_reason"] = failure_reason
        if package_handle is not UNSET:
            field_dict["package_handle"] = package_handle
        if package_sha256 is not UNSET:
            field_dict["package_sha256"] = package_sha256
        if publication_commit is not UNSET:
            field_dict["publication_commit"] = publication_commit
        if release_released_at is not UNSET:
            field_dict["release_released_at"] = release_released_at
        if release_version is not UNSET:
            field_dict["release_version"] = release_version
        if source_bundle_sha256 is not UNSET:
            field_dict["source_bundle_sha256"] = source_bundle_sha256
        if source_path is not UNSET:
            field_dict["source_path"] = source_path

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_package_handle_projection import RecipePackageHandleProjection # noqa: PLC0415
        d = dict(src_dict)
        def _parse_failure_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure_reason = _parse_failure_reason(d.pop("failure_reason", UNSET))


        def _parse_package_handle(data: object) -> None | RecipePackageHandleProjection | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                package_handle_type_0 = RecipePackageHandleProjection.from_dict(data)



                return package_handle_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipePackageHandleProjection | Unset, data)

        package_handle = _parse_package_handle(d.pop("package_handle", UNSET))


        def _parse_package_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        package_sha256 = _parse_package_sha256(d.pop("package_sha256", UNSET))


        def _parse_publication_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        publication_commit = _parse_publication_commit(d.pop("publication_commit", UNSET))


        def _parse_release_released_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        release_released_at = _parse_release_released_at(d.pop("release_released_at", UNSET))


        def _parse_release_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        release_version = _parse_release_version(d.pop("release_version", UNSET))


        def _parse_source_bundle_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_bundle_sha256 = _parse_source_bundle_sha256(d.pop("source_bundle_sha256", UNSET))


        def _parse_source_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_path = _parse_source_path(d.pop("source_path", UNSET))


        unprojected_revision = cls(
            failure_reason=failure_reason,
            package_handle=package_handle,
            package_sha256=package_sha256,
            publication_commit=publication_commit,
            release_released_at=release_released_at,
            release_version=release_version,
            source_bundle_sha256=source_bundle_sha256,
            source_path=source_path,
        )

        return unprojected_revision
