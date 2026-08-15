from typing import Literal, cast

PlacementEvidenceCountsTruncatedCollectionsItem = Literal['builds', 'installation_members', 'installations', 'mapping_members', 'mappings', 'run_members', 'runs']

PLACEMENT_EVIDENCE_COUNTS_TRUNCATED_COLLECTIONS_ITEM_VALUES: set[PlacementEvidenceCountsTruncatedCollectionsItem] = { 'builds', 'installation_members', 'installations', 'mapping_members', 'mappings', 'run_members', 'runs',  }

def check_placement_evidence_counts_truncated_collections_item(value: str) -> PlacementEvidenceCountsTruncatedCollectionsItem:
    if value in PLACEMENT_EVIDENCE_COUNTS_TRUNCATED_COLLECTIONS_ITEM_VALUES:
        return cast(PlacementEvidenceCountsTruncatedCollectionsItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLACEMENT_EVIDENCE_COUNTS_TRUNCATED_COLLECTIONS_ITEM_VALUES!r}")
