from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.workload_provenance_mapping_agreement import check_workload_provenance_mapping_agreement
from ..models.workload_provenance_mapping_agreement import WorkloadProvenanceMappingAgreement
from ..models.workload_provenance_rank_agreement import check_workload_provenance_rank_agreement
from ..models.workload_provenance_rank_agreement import WorkloadProvenanceRankAgreement
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.deployment_model_identity import DeploymentModelIdentity
  from ..models.physical_acceptance_evidence import PhysicalAcceptanceEvidence
  from ..models.rank_provenance import RankProvenance





T = TypeVar("T", bound="WorkloadProvenance")



@_attrs_define
class WorkloadProvenance:
    """
        Attributes:
            image_digest (str):
            installation_id (str):
            installation_state (str):
            mapping_agreement (WorkloadProvenanceMappingAgreement):
            mapping_generation (int):
            mapping_id (str):
            models (list[DeploymentModelIdentity]):
            physical_acceptance (PhysicalAcceptanceEvidence):
            rank_agreement (WorkloadProvenanceRankAgreement):
            ranks (list[RankProvenance]):
            recipe_content_sha256 (str):
            recipe_publisher (str):
            recipe_revision_id (str):
            recipe_revision_number (int):
            recipe_slug (str):
            build_id (None | str | Unset):
            build_input_sha256 (None | str | Unset):
            current_mapping_generation (int | None | Unset):
            run_generation (int | None | Unset):
            run_id (None | str | Unset):
            run_state (None | str | Unset):
            source_bundle_sha256 (None | str | Unset):
     """

    image_digest: str
    installation_id: str
    installation_state: str
    mapping_agreement: WorkloadProvenanceMappingAgreement
    mapping_generation: int
    mapping_id: str
    models: list[DeploymentModelIdentity]
    physical_acceptance: PhysicalAcceptanceEvidence
    rank_agreement: WorkloadProvenanceRankAgreement
    ranks: list[RankProvenance]
    recipe_content_sha256: str
    recipe_publisher: str
    recipe_revision_id: str
    recipe_revision_number: int
    recipe_slug: str
    build_id: None | str | Unset = UNSET
    build_input_sha256: None | str | Unset = UNSET
    current_mapping_generation: int | None | Unset = UNSET
    run_generation: int | None | Unset = UNSET
    run_id: None | str | Unset = UNSET
    run_state: None | str | Unset = UNSET
    source_bundle_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.deployment_model_identity import DeploymentModelIdentity # noqa: PLC0415
        from ..models.physical_acceptance_evidence import PhysicalAcceptanceEvidence # noqa: PLC0415
        from ..models.rank_provenance import RankProvenance # noqa: PLC0415
        image_digest = self.image_digest

        installation_id = self.installation_id

        installation_state = self.installation_state

        mapping_agreement: str = self.mapping_agreement

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        models = []
        for models_item_data in self.models:
            models_item = models_item_data.to_dict()
            models.append(models_item)



        physical_acceptance = self.physical_acceptance.to_dict()

        rank_agreement: str = self.rank_agreement

        ranks = []
        for ranks_item_data in self.ranks:
            ranks_item = ranks_item_data.to_dict()
            ranks.append(ranks_item)



        recipe_content_sha256 = self.recipe_content_sha256

        recipe_publisher = self.recipe_publisher

        recipe_revision_id = self.recipe_revision_id

        recipe_revision_number = self.recipe_revision_number

        recipe_slug = self.recipe_slug

        build_id: None | str | Unset
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        current_mapping_generation: int | None | Unset
        if isinstance(self.current_mapping_generation, Unset):
            current_mapping_generation = UNSET
        else:
            current_mapping_generation = self.current_mapping_generation

        run_generation: int | None | Unset
        if isinstance(self.run_generation, Unset):
            run_generation = UNSET
        else:
            run_generation = self.run_generation

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        run_state: None | str | Unset
        if isinstance(self.run_state, Unset):
            run_state = UNSET
        else:
            run_state = self.run_state

        source_bundle_sha256: None | str | Unset
        if isinstance(self.source_bundle_sha256, Unset):
            source_bundle_sha256 = UNSET
        else:
            source_bundle_sha256 = self.source_bundle_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_digest": image_digest,
            "installation_id": installation_id,
            "installation_state": installation_state,
            "mapping_agreement": mapping_agreement,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "models": models,
            "physical_acceptance": physical_acceptance,
            "rank_agreement": rank_agreement,
            "ranks": ranks,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_publisher": recipe_publisher,
            "recipe_revision_id": recipe_revision_id,
            "recipe_revision_number": recipe_revision_number,
            "recipe_slug": recipe_slug,
        })
        if build_id is not UNSET:
            field_dict["build_id"] = build_id
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if current_mapping_generation is not UNSET:
            field_dict["current_mapping_generation"] = current_mapping_generation
        if run_generation is not UNSET:
            field_dict["run_generation"] = run_generation
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if run_state is not UNSET:
            field_dict["run_state"] = run_state
        if source_bundle_sha256 is not UNSET:
            field_dict["source_bundle_sha256"] = source_bundle_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deployment_model_identity import DeploymentModelIdentity # noqa: PLC0415
        from ..models.physical_acceptance_evidence import PhysicalAcceptanceEvidence # noqa: PLC0415
        from ..models.rank_provenance import RankProvenance # noqa: PLC0415
        d = dict(src_dict)
        image_digest = d.pop("image_digest")

        installation_id = d.pop("installation_id")

        installation_state = d.pop("installation_state")

        mapping_agreement = check_workload_provenance_mapping_agreement(d.pop("mapping_agreement"))




        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        models = []
        _models = d.pop("models")
        for models_item_data in (_models):
            models_item = DeploymentModelIdentity.from_dict(models_item_data)



            models.append(models_item)


        physical_acceptance = PhysicalAcceptanceEvidence.from_dict(d.pop("physical_acceptance"))




        rank_agreement = check_workload_provenance_rank_agreement(d.pop("rank_agreement"))




        ranks = []
        _ranks = d.pop("ranks")
        for ranks_item_data in (_ranks):
            ranks_item = RankProvenance.from_dict(ranks_item_data)



            ranks.append(ranks_item)


        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_publisher = d.pop("recipe_publisher")

        recipe_revision_id = d.pop("recipe_revision_id")

        recipe_revision_number = d.pop("recipe_revision_number")

        recipe_slug = d.pop("recipe_slug")

        def _parse_build_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_current_mapping_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        current_mapping_generation = _parse_current_mapping_generation(d.pop("current_mapping_generation", UNSET))


        def _parse_run_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        run_generation = _parse_run_generation(d.pop("run_generation", UNSET))


        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))


        def _parse_run_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_state = _parse_run_state(d.pop("run_state", UNSET))


        def _parse_source_bundle_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_bundle_sha256 = _parse_source_bundle_sha256(d.pop("source_bundle_sha256", UNSET))


        workload_provenance = cls(
            image_digest=image_digest,
            installation_id=installation_id,
            installation_state=installation_state,
            mapping_agreement=mapping_agreement,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            models=models,
            physical_acceptance=physical_acceptance,
            rank_agreement=rank_agreement,
            ranks=ranks,
            recipe_content_sha256=recipe_content_sha256,
            recipe_publisher=recipe_publisher,
            recipe_revision_id=recipe_revision_id,
            recipe_revision_number=recipe_revision_number,
            recipe_slug=recipe_slug,
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            current_mapping_generation=current_mapping_generation,
            run_generation=run_generation,
            run_id=run_id,
            run_state=run_state,
            source_bundle_sha256=source_bundle_sha256,
        )

        return workload_provenance
