from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.placement_evidence_counts_truncated_collections_item import check_placement_evidence_counts_truncated_collections_item
from ..models.placement_evidence_counts_truncated_collections_item import PlacementEvidenceCountsTruncatedCollectionsItem
from typing import cast






T = TypeVar("T", bound="PlacementEvidenceCounts")



@_attrs_define
class PlacementEvidenceCounts:
    """
        Attributes:
            builds (int):
            installation_members (int):
            installations (int):
            mapping_members (int):
            mappings (int):
            run_members (int):
            runs (int):
            truncated_collections (list[PlacementEvidenceCountsTruncatedCollectionsItem]):
     """

    builds: int
    installation_members: int
    installations: int
    mapping_members: int
    mappings: int
    run_members: int
    runs: int
    truncated_collections: list[PlacementEvidenceCountsTruncatedCollectionsItem]





    def to_dict(self) -> dict[str, Any]:
        builds = self.builds

        installation_members = self.installation_members

        installations = self.installations

        mapping_members = self.mapping_members

        mappings = self.mappings

        run_members = self.run_members

        runs = self.runs

        truncated_collections = []
        for truncated_collections_item_data in self.truncated_collections:
            truncated_collections_item: str = truncated_collections_item_data
            truncated_collections.append(truncated_collections_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "builds": builds,
            "installation_members": installation_members,
            "installations": installations,
            "mapping_members": mapping_members,
            "mappings": mappings,
            "run_members": run_members,
            "runs": runs,
            "truncated_collections": truncated_collections,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        builds = d.pop("builds")

        installation_members = d.pop("installation_members")

        installations = d.pop("installations")

        mapping_members = d.pop("mapping_members")

        mappings = d.pop("mappings")

        run_members = d.pop("run_members")

        runs = d.pop("runs")

        truncated_collections = []
        _truncated_collections = d.pop("truncated_collections")
        for truncated_collections_item_data in (_truncated_collections):
            truncated_collections_item = check_placement_evidence_counts_truncated_collections_item(truncated_collections_item_data)



            truncated_collections.append(truncated_collections_item)


        placement_evidence_counts = cls(
            builds=builds,
            installation_members=installation_members,
            installations=installations,
            mapping_members=mapping_members,
            mappings=mappings,
            run_members=run_members,
            runs=runs,
            truncated_collections=truncated_collections,
        )

        return placement_evidence_counts
