from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.workload_provenance_mapping_agreement import check_workload_provenance_mapping_agreement
from ..models.workload_provenance_mapping_agreement import WorkloadProvenanceMappingAgreement
from ..models.workload_provenance_rank_agreement import check_workload_provenance_rank_agreement
from ..models.workload_provenance_rank_agreement import WorkloadProvenanceRankAgreement
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.deployment_model_identity import DeploymentModelIdentity
  from ..models.rank_provenance import RankProvenance
  from ..models.physical_acceptance_evidence import PhysicalAcceptanceEvidence





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
            models (list['DeploymentModelIdentity']):
            physical_acceptance (PhysicalAcceptanceEvidence):
            rank_agreement (WorkloadProvenanceRankAgreement):
            ranks (list['RankProvenance']):
            recipe_content_sha256 (str):
            recipe_publisher (str):
            recipe_revision_id (str):
            recipe_revision_number (int):
            recipe_slug (str):
            build_id (Union[None, Unset, str]):
            build_input_sha256 (Union[None, Unset, str]):
            current_mapping_generation (Union[None, Unset, int]):
            run_generation (Union[None, Unset, int]):
            run_id (Union[None, Unset, str]):
            run_state (Union[None, Unset, str]):
            source_bundle_sha256 (Union[None, Unset, str]):
     """

    image_digest: str
    installation_id: str
    installation_state: str
    mapping_agreement: WorkloadProvenanceMappingAgreement
    mapping_generation: int
    mapping_id: str
    models: list['DeploymentModelIdentity']
    physical_acceptance: 'PhysicalAcceptanceEvidence'
    rank_agreement: WorkloadProvenanceRankAgreement
    ranks: list['RankProvenance']
    recipe_content_sha256: str
    recipe_publisher: str
    recipe_revision_id: str
    recipe_revision_number: int
    recipe_slug: str
    build_id: Union[None, Unset, str] = UNSET
    build_input_sha256: Union[None, Unset, str] = UNSET
    current_mapping_generation: Union[None, Unset, int] = UNSET
    run_generation: Union[None, Unset, int] = UNSET
    run_id: Union[None, Unset, str] = UNSET
    run_state: Union[None, Unset, str] = UNSET
    source_bundle_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.deployment_model_identity import DeploymentModelIdentity
        from ..models.rank_provenance import RankProvenance
        from ..models.physical_acceptance_evidence import PhysicalAcceptanceEvidence
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

        build_id: Union[None, Unset, str]
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id

        build_input_sha256: Union[None, Unset, str]
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        current_mapping_generation: Union[None, Unset, int]
        if isinstance(self.current_mapping_generation, Unset):
            current_mapping_generation = UNSET
        else:
            current_mapping_generation = self.current_mapping_generation

        run_generation: Union[None, Unset, int]
        if isinstance(self.run_generation, Unset):
            run_generation = UNSET
        else:
            run_generation = self.run_generation

        run_id: Union[None, Unset, str]
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        run_state: Union[None, Unset, str]
        if isinstance(self.run_state, Unset):
            run_state = UNSET
        else:
            run_state = self.run_state

        source_bundle_sha256: Union[None, Unset, str]
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
        from ..models.deployment_model_identity import DeploymentModelIdentity
        from ..models.rank_provenance import RankProvenance
        from ..models.physical_acceptance_evidence import PhysicalAcceptanceEvidence
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

        def _parse_build_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        def _parse_build_input_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_current_mapping_generation(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        current_mapping_generation = _parse_current_mapping_generation(d.pop("current_mapping_generation", UNSET))


        def _parse_run_generation(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        run_generation = _parse_run_generation(d.pop("run_generation", UNSET))


        def _parse_run_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))


        def _parse_run_state(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        run_state = _parse_run_state(d.pop("run_state", UNSET))


        def _parse_source_bundle_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

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
