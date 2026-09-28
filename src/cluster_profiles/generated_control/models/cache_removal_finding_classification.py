from typing import Literal

CacheRemovalFindingClassification = Literal['active-work', 'saved-reference']

CACHE_REMOVAL_FINDING_CLASSIFICATION_VALUES: set[CacheRemovalFindingClassification] = { 'active-work', 'saved-reference',  }

def check_cache_removal_finding_classification(value: str) -> CacheRemovalFindingClassification:
    if value in CACHE_REMOVAL_FINDING_CLASSIFICATION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_REMOVAL_FINDING_CLASSIFICATION_VALUES!r}")
