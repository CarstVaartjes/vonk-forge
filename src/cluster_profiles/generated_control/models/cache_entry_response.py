from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.cache_entry_response_coverage import CacheEntryResponseCoverage
from ..models.cache_entry_response_coverage import check_cache_entry_response_coverage
from ..models.cache_entry_response_state import CacheEntryResponseState
from ..models.cache_entry_response_state import check_cache_entry_response_state
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.cache_artifact_response import CacheArtifactResponse





T = TypeVar("T", bound="CacheEntryResponse")



@_attrs_define
class CacheEntryResponse:
    """
        Attributes:
            artifact_set_sha256 (str):
            artifacts (list['CacheArtifactResponse']):
            coverage (CacheEntryResponseCoverage):
            created_at (str):
            expected_bytes (int):
            model_version_sha256 (Union[None, str]):
            protected (bool):
            protected_reasons (list[str]):
            recipe_revision_sha256 (Union[None, str]):
            recipe_update_available (bool):
            state (CacheEntryResponseState):
            unique_bytes (int):
            update_available (bool):
            updated_at (str):
            verified_at (Union[None, str]):
            verified_bytes (int):
            last_error (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    artifact_set_sha256: str
    artifacts: list['CacheArtifactResponse']
    coverage: CacheEntryResponseCoverage
    created_at: str
    expected_bytes: int
    model_version_sha256: Union[None, str]
    protected: bool
    protected_reasons: list[str]
    recipe_revision_sha256: Union[None, str]
    recipe_update_available: bool
    state: CacheEntryResponseState
    unique_bytes: int
    update_available: bool
    updated_at: str
    verified_at: Union[None, str]
    verified_bytes: int
    last_error: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.cache_artifact_response import CacheArtifactResponse
        artifact_set_sha256 = self.artifact_set_sha256

        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        coverage: str = self.coverage

        created_at = self.created_at

        expected_bytes = self.expected_bytes

        model_version_sha256: Union[None, str]
        model_version_sha256 = self.model_version_sha256

        protected = self.protected

        protected_reasons = self.protected_reasons



        recipe_revision_sha256: Union[None, str]
        recipe_revision_sha256 = self.recipe_revision_sha256

        recipe_update_available = self.recipe_update_available

        state: str = self.state

        unique_bytes = self.unique_bytes

        update_available = self.update_available

        updated_at = self.updated_at

        verified_at: Union[None, str]
        verified_at = self.verified_at

        verified_bytes = self.verified_bytes

        last_error: Union[None, Unset, str]
        if isinstance(self.last_error, Unset):
            last_error = UNSET
        else:
            last_error = self.last_error

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "artifacts": artifacts,
            "coverage": coverage,
            "created_at": created_at,
            "expected_bytes": expected_bytes,
            "model_version_sha256": model_version_sha256,
            "protected": protected,
            "protected_reasons": protected_reasons,
            "recipe_revision_sha256": recipe_revision_sha256,
            "recipe_update_available": recipe_update_available,
            "state": state,
            "unique_bytes": unique_bytes,
            "update_available": update_available,
            "updated_at": updated_at,
            "verified_at": verified_at,
            "verified_bytes": verified_bytes,
        })
        if last_error is not UNSET:
            field_dict["last_error"] = last_error
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cache_artifact_response import CacheArtifactResponse
        d = dict(src_dict)
        artifact_set_sha256 = d.pop("artifact_set_sha256")

        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = CacheArtifactResponse.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        coverage = check_cache_entry_response_coverage(d.pop("coverage"))




        created_at = d.pop("created_at")

        expected_bytes = d.pop("expected_bytes")

        def _parse_model_version_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        model_version_sha256 = _parse_model_version_sha256(d.pop("model_version_sha256"))


        protected = d.pop("protected")

        protected_reasons = cast(list[str], d.pop("protected_reasons"))


        def _parse_recipe_revision_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_revision_sha256 = _parse_recipe_revision_sha256(d.pop("recipe_revision_sha256"))


        recipe_update_available = d.pop("recipe_update_available")

        state = check_cache_entry_response_state(d.pop("state"))




        unique_bytes = d.pop("unique_bytes")

        update_available = d.pop("update_available")

        updated_at = d.pop("updated_at")

        def _parse_verified_at(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        verified_at = _parse_verified_at(d.pop("verified_at"))


        verified_bytes = d.pop("verified_bytes")

        def _parse_last_error(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        last_error = _parse_last_error(d.pop("last_error", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        cache_entry_response = cls(
            artifact_set_sha256=artifact_set_sha256,
            artifacts=artifacts,
            coverage=coverage,
            created_at=created_at,
            expected_bytes=expected_bytes,
            model_version_sha256=model_version_sha256,
            protected=protected,
            protected_reasons=protected_reasons,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_update_available=recipe_update_available,
            state=state,
            unique_bytes=unique_bytes,
            update_available=update_available,
            updated_at=updated_at,
            verified_at=verified_at,
            verified_bytes=verified_bytes,
            last_error=last_error,
            schema_version=schema_version,
        )

        return cache_entry_response
